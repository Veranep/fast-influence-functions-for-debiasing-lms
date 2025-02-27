# Copyright (c) 2020, salesforce.com, inc.
# All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# For full license text, see the LICENSE file in the repo root or https://opensource.org/licenses/BSD-3-Clause

import os
import torch
import numpy as np

# from tqdm import tqdm
from torch.utils.data.dataloader import DataLoader
from torch.utils.data.sampler import SequentialSampler, RandomSampler
from typing import Tuple, Optional, Union, Any, Dict, List, Callable
from transformers import (
    AutoModelForSequenceClassification,
    AutoTokenizer,
    BertTokenizer,
    BertForSequenceClassification,
    GlueDataTrainingArguments,
    Trainer,
    DataCollator,
    default_data_collator,
)

from influence_utils import glue_utils
from experiments import constants
from experiments.data_utils import CustomGlueDataset


def sort_dict_keys_by_vals(d: Dict[int, float]) -> List[int]:
    sorted_items = sorted(list(d.items()), key=lambda pair: pair[1])
    return [pair[0] for pair in sorted_items]


def sort_dict_keys_by_vals_with_conditions(
    d: Dict[int, float], condition_func: Callable[[Tuple[int, float]], bool]
) -> List[int]:

    sorted_items = sorted(list(d.items()), key=lambda pair: pair[1])
    return [pair[0] for pair in sorted_items if condition_func(pair)]


def get_helpful_harmful_indices_from_influences_dict(
    d: Dict[int, float],
    n: Optional[int] = None,
) -> Tuple[List[int], List[int]]:

    helpful_indices = sort_dict_keys_by_vals_with_conditions(
        d, condition_func=lambda k_v: k_v[1] < 0.0
    )
    harmful_indices = sort_dict_keys_by_vals_with_conditions(
        d, condition_func=lambda k_v: k_v[1] > 0.0
    )[::-1]

    if n is not None:
        if len(helpful_indices) < n:
            raise ValueError(
                f"`helpful_indices` have only "
                f"{len(helpful_indices)} elememts "
                f"whereas {n} is needed"
            )

        if len(harmful_indices) < n:
            raise ValueError(
                f"`harmful_indices` have only "
                f"{len(harmful_indices)} elememts "
                f"whereas {n} is needed"
            )

        helpful_indices = helpful_indices[:n]
        harmful_indices = harmful_indices[:n]

    return helpful_indices, harmful_indices


def compute_BERT_CLS_feature(
    model,
    input_ids=None,
    attention_mask=None,
    token_type_ids=None,
    labels=None,
) -> torch.FloatTensor:
    r"""
    labels (:obj:`torch.LongTensor` of shape :obj:`(batch_size,)`, `optional`, defaults to :obj:`None`):
        Labels for computing the sequence classification/regression loss.
        Indices should be in :obj:`[0, ..., config.num_labels - 1]`.
        If :obj:`config.num_labels == 1` a regression loss is computed (Mean-Square loss),
        If :obj:`config.num_labels > 1` a classification loss is computed (Cross-Entropy).
    """
    if model.training is True:
        raise ValueError
    if hasattr(model, "bert"):
        outputs = model.bert(
            input_ids.reshape([-1, input_ids.shape[-1]]),
            attention_mask=attention_mask.reshape(
                [-1, attention_mask.shape[-1]]
            ),
            token_type_ids=token_type_ids.reshape(
                [-1, token_type_ids.shape[-1]]
            ),
        )
    elif hasattr(model, "distilbert"):
        outputs = model.distilbert(
            input_ids.reshape([-1, input_ids.shape[-1]]),
            attention_mask=attention_mask.reshape(
                [-1, attention_mask.shape[-1]]
            ),
        )
    elif hasattr(model, "roberta"):
        outputs = model.roberta(
            input_ids.reshape([-1, input_ids.shape[-1]]),
            attention_mask=attention_mask.reshape(
                [-1, attention_mask.shape[-1]]
            ),
        )
    elif hasattr(model, "deberta"):
        outputs = model.deberta(
            input_ids.reshape([-1, input_ids.shape[-1]]),
            attention_mask=attention_mask.reshape(
                [-1, attention_mask.shape[-1]]
            ),
        )
    else:
        outputs = model.model.encoder(
            input_ids.reshape([-1, input_ids.shape[-1]]),
            attention_mask=attention_mask.reshape(
                [-1, attention_mask.shape[-1]]
            ),
        )
    output = outputs[0][:, -1, :]
    return output
    # return model.dropout(output)


def create_tokenizer_and_model(
    model_name_or_path: str, freeze_parameters: bool = True
) -> Tuple[BertTokenizer, BertForSequenceClassification]:
    if model_name_or_path is None:
        raise ValueError
    tokenizer = AutoTokenizer.from_pretrained(model_name_or_path)
    model = AutoModelForSequenceClassification.from_pretrained(
        model_name_or_path
    )

    model.eval()
    if freeze_parameters is True:
        glue_utils.freeze_BERT_parameters(model)

    return tokenizer, model


def create_datasets(
    task_name: str,
    tokenizer: BertTokenizer,
    data_dir: Optional[str] = None,
    create_test_dataset: bool = False,
) -> Union[
    Tuple[CustomGlueDataset, CustomGlueDataset],
    Tuple[CustomGlueDataset, CustomGlueDataset, CustomGlueDataset],
]:
    if task_name not in ["mnli", "mnli-2", "hans", "amazon", "anli"]:
        raise ValueError(f"Unrecognized task {task_name}")

    if data_dir is None:
        if task_name in ["mnli", "mnli-2"]:
            data_dir = constants.GLUE_DATA_DIR
        if task_name in ["hans"]:
            data_dir = constants.HANS_DATA_DIR
        if task_name in ["amazon"]:
            data_dir = constants.Amazon_DATA_DIR
        if task_name in ["anli"]:
            data_dir = constants.ANLI_DATA_DIR

    data_args = GlueDataTrainingArguments(
        task_name=task_name, data_dir=data_dir, max_seq_length=128
    )

    train_dataset = CustomGlueDataset(
        args=data_args, tokenizer=tokenizer, mode="train"
    )

    eval_dataset = CustomGlueDataset(
        args=data_args, tokenizer=tokenizer, mode="dev"
    )

    if create_test_dataset is False:
        return train_dataset, eval_dataset
    else:
        test_dataset = CustomGlueDataset(
            args=data_args, tokenizer=tokenizer, mode="test"
        )

        return train_dataset, eval_dataset, test_dataset


def predict(
    trainer: Trainer,
    model: torch.nn.Module,
    inputs: Dict[str, Union[torch.Tensor, Any]],
) -> Tuple[np.ndarray, np.ndarray, Optional[float]]:

    if trainer.args.past_index >= 0:
        raise ValueError

    has_labels = any(
        inputs.get(k) is not None
        for k in ["labels", "lm_labels", "masked_lm_labels"]
    )

    for k, v in inputs.items():
        if isinstance(v, torch.Tensor):
            inputs[k] = v.to(trainer.args.device)

    step_eval_loss = None
    with torch.no_grad():
        # added:
        inputs["labels"] -= 1
        outputs = model(**inputs)
        if has_labels:
            step_eval_loss, logits = outputs[:2]
        else:
            logits = outputs[0]

    preds = logits.detach()
    preds = preds.cpu().numpy()
    if inputs.get("labels") is not None:
        label_ids = inputs["labels"].detach()
        label_ids = label_ids.cpu().numpy()

    if step_eval_loss is not None:
        step_eval_loss = step_eval_loss.mean().item()

    return preds, label_ids, step_eval_loss


def get_dataloader(
    dataset: CustomGlueDataset,
    batch_size: int,
    random: bool = False,
    data_collator: Optional[DataCollator] = None,
) -> DataLoader:
    if data_collator is None:
        data_collator = default_data_collator

    if random is True:
        sampler = RandomSampler(dataset)
    else:
        sampler = SequentialSampler(dataset)

    data_loader = DataLoader(
        dataset,
        sampler=sampler,
        batch_size=batch_size,
        collate_fn=data_collator,
    )

    return data_loader


def remove_file_if_exists(file_name: str) -> None:
    if os.path.exists(file_name):
        os.remove(file_name)
    else:
        print("The file does not exist")


def is_prediction_correct(
    trainer: Trainer,
    model: torch.nn.Module,
    inputs: Dict[str, Union[torch.Tensor, Any]],
) -> bool:

    preds, label_ids, step_eval_loss = predict(
        trainer=trainer, model=model, inputs=inputs
    )

    if preds.shape[0] != 1:
        raise ValueError("This function only works on instances.")

    return bool((preds.argmax(axis=-1) == label_ids).all())


def move_inputs_to_device(
    inputs: Dict[str, Any], device: torch.device
) -> None:
    for k, v in inputs.items():
        if isinstance(v, torch.Tensor):
            inputs[k] = v.to(device)
