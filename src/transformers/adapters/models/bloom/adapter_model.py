import logging
import warnings

from typing import Optional


import torch

from ....models.bloom.modeling_bloom import BLOOM_START_DOCSTRING, BloomModel, BloomPreTrainedModel
from ....utils import add_start_docstrings
from ...composition import adjust_tensors_for_parallel
from ...heads import (
    CausalLMHead,
    ClassificationHead,
    ModelWithFlexibleHeadsAdaptersMixin,
    MultiLabelClassificationHead,
    TaggingHead,
)
from ...model_mixin import EmbeddingAdaptersWrapperMixin


logger = logging.getLogger(__name__)


@add_start_docstrings(
    BLOOM_START_DOCSTRING,
)
class BloomAdapterModel(EmbeddingAdaptersWrapperMixin, ModelWithFlexibleHeadsAdaptersMixin, BloomPreTrainedModel):
    def __init__(self, config):
        super().__init__(config)
        self.transformer = BloomModel(config)
        self._init_head_modules()
        self.init_weights()

    def forward(
        self,
        input_ids=None,
        attention_mask=None,
        token_type_ids=None,
        position_ids=None,
        head_mask=None,
        inputs_embeds=None,
        use_cache=None,
        output_attentions=None,
        output_hidden_states=None,
        return_dict=None,
        head=None,
        output_adapter_gating_scores=False,
        output_adapter_fusion_attentions=False,
        **kwargs
    ):
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        outputs = self.transformer(
            input_ids,
            attention_mask=attention_mask,
            token_type_ids=token_type_ids,
            position_ids=position_ids,
            head_mask=head_mask,
            inputs_embeds=inputs_embeds,
            use_cache=use_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
            output_adapter_gating_scores=output_adapter_gating_scores,
            output_adapter_fusion_attentions=output_adapter_fusion_attentions,
            adapter_input_parallelized=kwargs.pop("adapter_input_parallelized", False),
        )

        batch_size = outputs[0].shape[0]

        if self.config.pad_token_id is None:
            # TODO-AH: this may result in unexpected behavior for classification. Find a better way to do this?
            sequence_lengths = -1
        else:
            if input_ids is not None:
                sequence_lengths = torch.ne(input_ids, self.config.pad_token_id).sum(-1) - 1
                (sequence_lengths,) = adjust_tensors_for_parallel(outputs[0], sequence_lengths)
            else:
                sequence_lengths = -1
                logger.warning(
                    f"{self.__class__.__name__} will not detect padding tokens in `inputs_embeds`. Results may be "
                    "unexpected if using padding tokens in conjunction with `inputs_embeds.`"
                )

        cls_logits = outputs[0][range(batch_size), sequence_lengths]

        outputs = self.forward_head(
            outputs,
            head_name=head,
            cls_output=cls_logits,
            attention_mask=attention_mask,
            return_dict=return_dict,
            **kwargs,
        )

        return outputs

    def prepare_inputs_for_generation(
        self,
        input_ids: torch.LongTensor,
        past_key_values: Optional[torch.Tensor] = None,
        attention_mask: Optional[torch.Tensor] = None,
        **kwargs
    ) -> dict:
        # only last token for input_ids if past is not None
        if past_key_values:
            input_ids = input_ids[:, -1].unsqueeze(-1)

            # the cache may be in the stardard format (e.g. in contrastive search), convert to bloom's format if needed
            if past_key_values[0][0].shape[0] == input_ids.shape[0]:
                past_key_values = self._convert_to_bloom_cache(past_key_values)

        return {
            "input_ids": input_ids,
            "past_key_values": past_key_values,
            "use_cache": kwargs.get("use_cache"),
            "attention_mask": attention_mask,
        }

    head_types = {
        "classification": ClassificationHead,
        "multilabel_classification": MultiLabelClassificationHead,
        "causal_lm": CausalLMHead,
        "tagging": TaggingHead,
    }

    def add_classification_head(
        self,
        head_name,
        num_labels=2,
        layers=2,
        activation_function="tanh",
        overwrite_ok=False,
        multilabel=False,
        id2label=None,
    ):
        """
        Adds a sequence classification head on top of the model.

        Args:
            head_name (str): The name of the head.
            num_labels (int, optional): Number of classification labels. Defaults to 2.
            layers (int, optional): Number of layers. Defaults to 2.
            activation_function (str, optional): Activation function. Defaults to 'tanh'.
            overwrite_ok (bool, optional): Force overwrite if a head with the same name exists. Defaults to False.
            multilabel (bool, optional): Enable multilabel classification setup. Defaults to False.
        """

        if multilabel:
            head = MultiLabelClassificationHead(self, head_name, num_labels, layers, activation_function, id2label)
        else:
            head = ClassificationHead(self, head_name, num_labels, layers, activation_function, id2label)
        self.add_prediction_head(head, overwrite_ok)

    def add_causal_lm_head(self, head_name, overwrite_ok=False):
        """
        Adds a causal language modeling head on top of the model.

        Args:
            head_name (str): The name of the head.
            overwrite_ok (bool, optional): Force overwrite if a head with the same name exists. Defaults to False.
        """
        head = CausalLMHead(self, head_name)
        self.add_prediction_head(head, overwrite_ok=overwrite_ok)
