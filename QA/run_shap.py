# coding=utf-8
# Copyright 2018 The Google AI Language Team Authors and The HuggingFace Inc. team.
# Copyright (c) 2018, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
""" Finetuning the library models for question-answering on SQuAD (DistilBERT, Bert, XLM, XLNet)."""


import argparse
import glob
import logging
import os
import shutil
import random
import timeit

import numpy as np
import torch
from torch.utils.data import DataLoader, RandomSampler, SequentialSampler
from torch.utils.data.distributed import DistributedSampler
from tqdm import tqdm, trange
from common.config import register_args, load_config_and_tokenizer
from functools import partial

from transformers import (
    MODEL_FOR_QUESTION_ANSWERING_MAPPING,
    WEIGHTS_NAME,
    AdamW,
    AutoConfig,
    AutoModelForQuestionAnswering,
    AutoTokenizer,
    get_linear_schedule_with_warmup,
)
from transformers.modeling_roberta import create_position_ids_from_input_ids

from data.custom_squad_feature import custom_squad_convert_examples_to_features, SquadResult, SquadProcessor

from data.qa_metrics import (compute_predictions_logits,hotpot_evaluate,)

from run_qa import load_and_cache_examples, set_seed, to_list
from run_probe import remove_padding
from probe.probe_models import ProbeRobertaForQuestionAnswering
from probe.probe_utils import stats_of_layer_attribution, get_link_mask_by_thresholds, get_link_mask_by_token_thresholds
from int_grad.ig_qa_utils import compute_predictions_index_and_logits
from vis_tools.vis_utils import visualize_pruned_layer_attributions, merge_tokens_into_words
from vis_tools.vis_utils import visualize_token_attributions, merge_tokens_into_words
from itertools import combinations

logger = logging.getLogger(__name__)

MODEL_CONFIG_CLASSES = list(MODEL_FOR_QUESTION_ANSWERING_MAPPING.keys())
MODEL_TYPES = tuple(conf.model_type for conf in MODEL_CONFIG_CLASSES)

import itertools
from functools import reduce, partial
from run_int_grad import merge_predictions
from shap.local_method_utils import run_shap_attribution


def _mkdir_f(prefix):
    if os.path.exists(prefix):
        shutil.rmtree(prefix)
    os.makedirs(prefix)

def predict_with_mask(active_mask, tokenizer,  model, base_inputs, full_input_ids):
    input_ids = tokenizer.mask_token_id * torch.ones_like(full_input_ids)
    input_ids[0, active_mask == 1]  = full_input_ids[0, active_mask == 1]
    prob = model.probe_forward(**base_inputs, input_ids=input_ids)
    return prob

def run_shap(args, tokenizer, model, inputs, feature):    
    tokens = feature.tokens
    inputs['return_kl'] = False

    full_input_ids = inputs.pop('input_ids')
    full_positioin_ids = create_position_ids_from_input_ids(full_input_ids, tokenizer.pad_token_id).to(full_input_ids.device)

    # fix position id
    inputs['position_ids'] = full_positioin_ids
    # fix cls ? maybe    
    score_fn = partial(predict_with_mask, tokenizer=tokenizer, model=model, base_inputs=inputs, full_input_ids=full_input_ids)
    np_attribution = run_shap_attribution(args, len(tokens), score_fn).reshape((1,-1))
    return torch.from_numpy(np_attribution)


def predict_and_run_shap(args, batch, model, tokenizer, batch_features, batch_examples):
    model.eval()
    batch = tuple(t.to(args.device) for t in batch)
    # only allow batch size 1
    assert batch[0].size(0) == 1    
    # run predictions
    with torch.no_grad():
        inputs = {
            "input_ids": batch[0],
            "attention_mask": batch[1],
            "token_type_ids": batch[2],
        }

        if args.model_type in ["roberta", "distilbert", "camembert", "bart"]:
            del inputs["token_type_ids"]
        feature_indices = batch[3]
        outputs = model.restricted_forward(**inputs)

    batch_start_logits, batch_end_logits = outputs
    batch_results = []
    for i, feature_index in enumerate(feature_indices):
        eval_feature = batch_features[i]
        unique_id = int(eval_feature.unique_id)

        output = [to_list(output[i]) for output in outputs]
        start_logits, end_logits = output
        result = SquadResult(unique_id, start_logits, end_logits)
        batch_results.append(result)
    
    batch_prelim_results, batch_predictions = compute_predictions_index_and_logits(
        batch_examples,
        batch_features,
        batch_results,
        args.n_best_size,
        args.max_answer_length,
        args.do_lower_case,
        tokenizer,
        args.dataset
    )
    
    # run attributions
    batch_start_indexes = torch.LongTensor([x.start_index for x in batch_prelim_results]).to(args.device)
    batch_end_indexes = torch.LongTensor([x.end_index for x in batch_prelim_results]).to(args.device)
    
    # for data parallel 
    inputs = {
        "input_ids": batch[0],
        "attention_mask": batch[1],
        "token_type_ids": batch[2],        
        "start_indexes": batch_start_indexes,
        "end_indexes": batch_end_indexes,
        "final_start_logits": batch_start_logits,
        "final_end_logits": batch_end_logits,        
    }
    if args.model_type in ["roberta", "distilbert", "camembert", "bart"]:
        del inputs["token_type_ids"]
    
    with torch.no_grad():
        importances = run_shap(args, tokenizer, model, inputs, batch_features[0])

    return batch_predictions, batch_prelim_results, importances

def shap_interp(args, model, tokenizer, prefix=""):
    if not os.path.exists(args.interp_dir):
        os.makedirs(args.interp_dir)

    # fix the model
    model.requires_grad_(False)

    dataset, examples, features = load_and_cache_examples(args, tokenizer, evaluate=True, output_examples=True)
    
    # assume one on on mapping
    assert len(examples) == len(features)

    if not os.path.exists(args.output_dir) and args.local_rank in [-1, 0]:
        os.makedirs(args.output_dir)

    args.eval_batch_size = 1    
    eval_sampler = SequentialSampler(dataset)
    eval_dataloader = DataLoader(dataset, sampler=eval_sampler, batch_size=args.eval_batch_size)

    # Eval!
    logger.info("***** Running evaluation {} *****".format(prefix))
    logger.info("  Num examples = %d", len(dataset))
    logger.info("  Batch size = %d", args.eval_batch_size)

    all_predictions = []
    start_time = timeit.default_timer()

    for batch in tqdm(eval_dataloader, desc="Evaluating"):
       
        feature_indices = to_list(batch[3])
        batch_features = [features[i] for i in feature_indices]
        batch_examples = [examples[i] for i in feature_indices]
        # batch prem, batch predictions
        batch = remove_padding(batch, batch_features[0])
        batch_predictions, batch_prelim_results, batch_importances = predict_and_run_shap(
            args,
            batch,
            model,
            tokenizer,
            batch_features,
            batch_examples
        )
        dump_shap_info(args, batch_examples, batch_features, tokenizer, batch_predictions, batch_prelim_results, batch_importances)
        # lots of info, dump to files immediately        
        all_predictions.append(batch_predictions)

    evalTime = timeit.default_timer() - start_time
    logger.info("  Evaluation done in total %f secs (%f sec per example)", evalTime, evalTime / len(dataset))

    # Compute predictions
    # output_prediction_file =  os.path.join(args.output_dir, "predictions_{}.json".format(prefix))
    # output_nbest_file = os.path.join(args.output_dir, "nbest_predictions_{}.json".format(prefix))

    # XLNet and XLM use a more complex post-processing procedure
    # Compute the F1 and exact scores.
    all_predictions = merge_predictions(all_predictions)
    results = hotpot_evaluate(examples[:len(all_predictions)], all_predictions)
    print(results)
    return results


def dump_shap_info(args, examples, features, tokenizer, predictions, prelim_results, attributions):
    
    # attentions, attributions
    # N_Layer * B * N_HEAD * L * L
    attributions = attributions.detach().cpu().requires_grad_(False)

    for example, feature, prelim_result, attribution in zip(
        examples,
        features,
        prelim_results,
        torch.unbind(attributions)
    ):
        actual_len = len(feature.tokens)
        attribution = attribution[:actual_len].clone().detach()

        filename = os.path.join(args.interp_dir, f'{feature.example_index}-{feature.qas_id}.bin')
        prelim_result = prelim_result._asdict()
        prediction = predictions[example.qas_id]
        torch.save({'example': example, 'feature': feature, 'prediction': prediction, 'prelim_result': prelim_result,
            'attribution': attribution}, filename)

def ig_analyze(args, tokenizer):
    filenames = os.listdir(args.interp_dir)
    filenames.sort(key=lambda x: int(x.split('-')[0]))
    # print(len(filenames))
    datset_stats = []
    _mkdir_f(args.visual_dir)
    for fname in tqdm(filenames, desc='Visualizing'):
        interp_info = torch.load(os.path.join(args.interp_dir, fname))
        # datset_stats.append(stats_of_ig_interpretation(tokenizer, interp_info))
        visualize_token_attributions(args, tokenizer, interp_info)

def main():
    parser = argparse.ArgumentParser()
    register_args(parser)

    parser.add_argument("--do_vis", action="store_true", help="Whether to run vis on the dev set.")
    parser.add_argument("--interp_dir",default=None,type=str,required=True,help="The output directory where the model checkpoints and predictions will be written.")
    parser.add_argument("--visual_dir",default=None,type=str,help="The output visualization dir.")

    args = parser.parse_args()
    # Setup CUDA, GPU & distributed training
    if args.local_rank == -1 or args.no_cuda:
        device = torch.device("cuda" if torch.cuda.is_available() and not args.no_cuda else "cpu")
        args.n_gpu = 0 if args.no_cuda else torch.cuda.device_count()
    else:  # Initializes the distributed backend which will take care of sychronizing nodes/GPUs
        torch.cuda.set_device(args.local_rank)
        device = torch.device("cuda", args.local_rank)
        torch.distributed.init_process_group(backend="nccl")
        args.n_gpu = 1
    args.device = device

    # Setup logging
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(name)s -   %(message)s",
        datefmt="%m/%d/%Y %H:%M:%S",
        level=logging.INFO if args.local_rank in [-1, 0] else logging.WARN,
    )
    logger.warning(
        "Process rank: %s, device: %s, n_gpu: %s, distributed training: %s",
        args.local_rank,
        device,
        args.n_gpu,
        bool(args.local_rank != -1),
    )

    # Load pretrained model and tokenizer
    if args.local_rank not in [-1, 0]:
        # Make sure only the first process in distributed training will download model & vocab
        torch.distributed.barrier()

    args.model_type = args.model_type.lower()
    config, tokenizer = load_config_and_tokenizer(args)
    # Set seed
    set_seed(args)

    if args.do_vis:
        ig_analyze(args, tokenizer)
    else:
        # Evaluation - we can ask to evaluate all the checkpoints (sub-directories) in a directory
        logger.info("Loading checkpoint %s for evaluation", args.model_name_or_path)
        checkpoint = args.model_name_or_path
        logger.info("Evaluate the following checkpoints: %s", checkpoint)

        # Reload the model
        model = ProbeRobertaForQuestionAnswering.from_pretrained(checkpoint)  # , force_download=True)
        model.to(args.device)

        # Evaluate
        result = shap_interp(args, model, tokenizer, prefix="")
        logger.info("Results: {}".format(result))

        return result

if __name__ == "__main__":
    main()