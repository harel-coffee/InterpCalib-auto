import collections
import math

import torch.nn.functional as F
import numpy as np
from data.dataset_utils import get_prefix_tokens

from transformers.data.metrics.squad_metrics import get_final_text  
def _get_best_indexes(logits, n_best_size):
    """Get the n-best logits from a list."""
    index_and_score = sorted(enumerate(logits), key=lambda x: x[1], reverse=True)

    best_indexes = []
    for i in range(len(index_and_score)):
        if i >= n_best_size:
            break
        best_indexes.append(index_and_score[i][0])
    return best_indexes


def _compute_softmax(scores):
    """Compute softmax probability over raw logits."""
    if not scores:
        return []

    max_score = None
    for score in scores:
        if max_score is None or score > max_score:
            max_score = score

    exp_scores = []
    total_sum = 0.0
    for score in scores:
        x = math.exp(score - max_score)
        exp_scores.append(x)
        total_sum += x

    probs = []
    for score in exp_scores:
        probs.append(score / total_sum)
    return probs

def compute_predictions_index_and_logits(
    all_examples,
    all_features,
    all_results,
    n_best_size,
    max_answer_length,
    do_lower_case,
    tokenizer,
    dataset='hpqa',
):

    unique_id_to_result = {}
    for result in all_results:
        unique_id_to_result[result.unique_id] = result

    _PrelimPrediction = collections.namedtuple(  # pylint: disable=invalid-name
        "PrelimPrediction", ["feature_index", "start_index", "end_index", "start_logit", "end_logit"]
    )

    all_predictions = collections.OrderedDict()
    all_nbest_json = collections.OrderedDict()

    all_prelim_predictions = []
    for (example_index, example) in enumerate(all_examples):
        features = [all_features[example_index]]
        prelim_predictions = []
        # keep track of the minimum score of null start+end of position 0
        score_null = 1000000  # large and positive
        for (feature_index, feature) in enumerate(features):
            result = unique_id_to_result[feature.unique_id]
            start_indexes = _get_best_indexes(result.start_logits, n_best_size)
            end_indexes = _get_best_indexes(result.end_logits, n_best_size)
            # if we could have irrelevant answers, get the min score of irrelevant
            for start_index in start_indexes:
                for end_index in end_indexes:
                    # We could hypothetically create invalid predictions, e.g., predict
                    # that the start of the span is in the question. We throw out all
                    # invalid predictions.
                    if start_index >= len(feature.tokens):
                        continue
                    if end_index >= len(feature.tokens):
                        continue
                    if start_index not in feature.token_to_orig_map:
                        continue
                    if end_index not in feature.token_to_orig_map:
                        continue
                    if not feature.token_is_max_context.get(start_index, False):
                        continue
                    if end_index < start_index:
                        continue
                    length = end_index - start_index + 1
                    if length > max_answer_length:
                        continue
                    prelim_predictions.append(
                        _PrelimPrediction(
                            feature_index=feature_index,
                            start_index=start_index,
                            end_index=end_index,
                            start_logit=result.start_logits[start_index],
                            end_logit=result.end_logits[end_index],
                        )
                    )

        prelim_predictions = sorted(prelim_predictions, key=lambda x: (x.start_logit + x.end_logit), reverse=True)
        # make sure it's feasible
        if prelim_predictions:
            all_prelim_predictions.append(prelim_predictions[0])
        else:
            all_prelim_predictions.append(_PrelimPrediction(
                            feature_index=0,
                            start_index=0,
                            end_index=0,
                            start_logit=0.0,
                            end_logit=0.0,
                        ))
        _NbestPrediction = collections.namedtuple(  # pylint: disable=invalid-name
            "NbestPrediction", ["text", "start_logit", "end_logit"]
        )

        seen_predictions = {}
        nbest = []
        prefix_tokens = get_prefix_tokens(dataset, tokenizer)
        ex_doc_tokens = prefix_tokens + example.doc_tokens
        for pred in prelim_predictions:
            if len(nbest) >= n_best_size:
                break
            feature = features[pred.feature_index]
            if pred.start_index > 0:  # this is a non-null prediction
                tok_tokens = feature.tokens[pred.start_index : (pred.end_index + 1)]
                orig_doc_start = feature.token_to_orig_map[pred.start_index]
                orig_doc_end = feature.token_to_orig_map[pred.end_index]
                orig_tokens = ex_doc_tokens[orig_doc_start : (orig_doc_end + 1)]

                tok_text = tokenizer.convert_tokens_to_string(tok_tokens)

                # tok_text = " ".join(tok_tokens)
                #
                # # De-tokenize WordPieces that have been split off.
                # tok_text = tok_text.replace(" ##", "")
                # tok_text = tok_text.replace("##", "")

                # Clean whitespace
                tok_text = tok_text.strip()
                tok_text = " ".join(tok_text.split())
                orig_text = " ".join(orig_tokens)

                final_text = get_final_text(tok_text, orig_text, do_lower_case, False)
                if final_text in seen_predictions:
                    continue

                seen_predictions[final_text] = True
            else:
                final_text = ""
                seen_predictions[final_text] = True

            nbest.append(_NbestPrediction(text=final_text, start_logit=pred.start_logit, end_logit=pred.end_logit))

        # In very rare edge cases we could have no valid predictions. So we
        # just create a nonce prediction in this case to avoid failure.
        if not nbest:
            nbest.append(_NbestPrediction(text="empty", start_logit=0.0, end_logit=0.0))

        assert len(nbest) >= 1, "No valid predictions"

        total_scores = []
        best_non_null_entry = None
        for entry in nbest:
            total_scores.append(entry.start_logit + entry.end_logit)
            if not best_non_null_entry:
                if entry.text:
                    best_non_null_entry = entry

        probs = _compute_softmax(total_scores)

        nbest_json = []
        for (i, entry) in enumerate(nbest):
            output = collections.OrderedDict()
            output["text"] = entry.text
            output["probability"] = probs[i]
            output["start_logit"] = entry.start_logit
            output["end_logit"] = entry.end_logit
            nbest_json.append(output)

        assert len(nbest_json) >= 1, "No valid predictions"
        all_predictions[example.qas_id] = nbest_json[0]["text"]
        all_nbest_json[example.qas_id] = nbest_json
    return all_prelim_predictions, all_predictions


def stats_of_ig_interpretation(tokenizer, interp_info):
    attribution = interp_info['attribution']
    attention = interp_info['attention']
    feature = interp_info['feature']
    prelim_result = interp_info['prelim_result']
    # attribution = attribution.res
    n_layers, n_heads, n_tokens, _ = tuple(attribution.size())
    # print(interp_info['prediction'])
    # print(feature.tokens[prelim_result['start_index']:prelim_result['end_index']])
    # print(n_layers, n_heads, n_tokens)
    
    
    attribution = attribution.view([-1])
    # attribution = F.log_softmax(attribution, dim=0) 
    # attribution = F.softmax(attribution)    
    attribution_val = attribution.numpy()
    attribution_diff = np.sum(attribution_val)
    sorted_indexes = np.argsort(-1 * attribution_val)
    sorted_attribution_val = attribution_val[sorted_indexes]
    # print(sorted_attribution_val[:100])
    # accumulateds 
    # thres_indexes = [0.8, 0.9, 0.95]
    # thres_indexes = dict()
    threshold = 0.999999
    acc_attribution = 0.0
    first_k = 0
    for v in sorted_attribution_val:
        first_k += 1
        acc_attribution += v
        if acc_attribution >= attribution_diff * threshold:
            break
    num_positive = np.sum(attribution_val > 0)
    print(math.ceil(sorted_attribution_val.size * threshold), first_k)
    print(first_k, sorted_attribution_val.size, '{:2f}'.format(first_k/sorted_attribution_val.size))
    print(num_positive, sorted_attribution_val.size, '{:2f}'.format(num_positive/sorted_attribution_val.size))
    # exit()
    def decode_index(a):
        l = a // (n_heads * n_tokens * n_tokens)
        a = a % (n_heads * n_tokens * n_tokens)
        h = a // (n_tokens * n_tokens)
        a = a % ( n_tokens * n_tokens)
        s = a // n_tokens
        e = a % n_tokens
        return l, h, s, e

    # inspect first 100
    for i in sorted_indexes[:100]:
        l, h, s, e = decode_index(i)
        print('{}-{}-{}-{}, v: {:.4f}, {}  -->  {}'.format(l, h, s, e, attribution_val[i] / attribution_diff, feature.tokens[s], feature.tokens[e]))
    exit()
    return {}    
