from collections import defaultdict
from copy import deepcopy
from itertools import permutations
from typing import List

from transformers import (AutoTokenizer,
                          AutoModelForSequenceClassification,
                          AutoModelForSeq2SeqLM,
                          PreTrainedModel,
                          PreTrainedTokenizer,
                          T5ForConditionalGeneration)
import torch
from sentence_transformers import CrossEncoder
from .base import Reranker, Query, Text
from .similarity import SimilarityMatrixProvider
from pygaggle.model import (BatchTokenizer,
                            LongBatchEncoder,
                            QueryDocumentBatch,
                            DuoQueryDocumentBatch,
                            QueryDocumentBatchTokenizer,
                            SpecialTokensCleaner,
                            T5BatchTokenizer,
                            T5DuoBatchTokenizer,
                            greedy_decode)


__all__ = ['MonoT5',
           'DuoT5',
           'UnsupervisedTransformerReranker',
           'MonoBERT',
           'QuestionAnsweringTransformerReranker',
           'SentenceTransformersReranker']

prediction_tokens = {
        'castorini/monot5-base-msmarco':             ['▁false', '▁true'],
        'castorini/monot5-base-msmarco-10k':         ['▁false', '▁true'],
        'castorini/monot5-large-msmarco':            ['▁false', '▁true'],
        'castorini/monot5-large-msmarco-10k':        ['▁false', '▁true'],
        'castorini/monot5-base-med-msmarco':         ['▁false', '▁true'],
        'castorini/monot5-3b-med-msmarco':           ['▁false', '▁true'],
        'castorini/monot5-3b-msmarco-10k':           ['▁false', '▁true'],
        'unicamp-dl/mt5-13b-mmarco-100k':            ['▁false', '▁true'],
        'unicamp-dl/mt5-base-en-msmarco':            ['▁no'   , '▁yes'],
        'unicamp-dl/ptt5-base-pt-msmarco-10k-v2':    ['▁não'  , '▁sim'],
        'unicamp-dl/ptt5-base-pt-msmarco-100k-v2':   ['▁não'  , '▁sim'],
        'unicamp-dl/ptt5-base-en-pt-msmarco-100k-v2':['▁não'  , '▁sim'],
        'unicamp-dl/mt5-base-en-pt-msmarco-v2':      ['▁no'   , '▁yes'],
        'unicamp-dl/mt5-base-mmarco-v2':             ['▁no'   , '▁yes'],
        'unicamp-dl/mt5-base-en-pt-msmarco-v1':      ['▁no'   , '▁yes'],
        'unicamp-dl/mt5-base-mmarco-v1':             ['▁no'   , '▁yes'],
        'unicamp-dl/ptt5-base-pt-msmarco-10k-v1':    ['▁não'  , '▁sim'],
        'unicamp-dl/ptt5-base-pt-msmarco-100k-v1':   ['▁não'  , '▁sim'],
        'unicamp-dl/ptt5-base-en-pt-msmarco-10k-v1': ['▁não'  , '▁sim'],
        'unicamp-dl/mt5-3B-mmarco-en-pt':            ['▁'  , '▁true']
        }


class MonoT5(Reranker):
    def __init__(self, 
                 pretrained_model_name_or_path: str  = 'castorini/monot5-base-msmarco-10k',
                 model: T5ForConditionalGeneration = None,
                 tokenizer: QueryDocumentBatchTokenizer = None,
                 use_amp = False,
                 token_false = None,
                 token_true  = None):
        self.model = model or self.get_model(pretrained_model_name_or_path)
        self.tokenizer = tokenizer or self.get_tokenizer(pretrained_model_name_or_path)
        self.token_false_id, self.token_true_id = self.get_prediction_tokens(
                pretrained_model_name_or_path, self.tokenizer, token_false, token_true)
        self.pretrained_model_name_or_path = pretrained_model_name_or_path
        self.device = next(self.model.parameters(), None).device
        self.use_amp = use_amp

    @staticmethod
    def get_model(pretrained_model_name_or_path: str,
                  *args, device: str = None, **kwargs) -> T5ForConditionalGeneration:
        device = device or ('cuda' if torch.cuda.is_available() else 'cpu')
        device = torch.device(device)
        return AutoModelForSeq2SeqLM.from_pretrained(pretrained_model_name_or_path,
                                                          *args, **kwargs).to(device).eval()

    @staticmethod
    def get_tokenizer(pretrained_model_name_or_path: str,
                      *args, batch_size: int = 8, **kwargs) -> T5BatchTokenizer:
        return T5BatchTokenizer(
            AutoTokenizer.from_pretrained(pretrained_model_name_or_path, use_fast=False, *args, **kwargs),
            batch_size=batch_size
        )
    @staticmethod
    def get_prediction_tokens(pretrained_model_name_or_path: str,
            tokenizer, token_false, token_true):
        if not (token_false and token_true):
            if pretrained_model_name_or_path in prediction_tokens:
                token_false, token_true = prediction_tokens[pretrained_model_name_or_path]
                token_false_id = tokenizer.tokenizer.get_vocab()[token_false]
                token_true_id  = tokenizer.tokenizer.get_vocab()[token_true]
                return token_false_id, token_true_id
            else:
                raise Exception(f"We don't know the indexes for the non-relevant/relevant tokens for\
                        the checkpoint {pretrained_model_name_or_path} and you did not provide any.")
        else:
            token_false_id = tokenizer.tokenizer.get_vocab()[token_false]
            token_true_id  = tokenizer.tokenizer.get_vocab()[token_true]
            return token_false_id, token_true_id


    def rescore(self, query: Query, texts: List[Text]) -> List[Text]:
        texts = deepcopy(texts)
        batch_input = QueryDocumentBatch(query=query, documents=texts)
        for batch in self.tokenizer.traverse_query_document(batch_input):
            with torch.cuda.amp.autocast(enabled=self.use_amp):
                input_ids = batch.output['input_ids'].to(self.device)
                attn_mask = batch.output['attention_mask'].to(self.device)
                _, batch_scores = greedy_decode(self.model,
                                                input_ids,
                                                length=1,
                                                attention_mask=attn_mask,
                                                return_last_logits=True)

                batch_scores = batch_scores[:, [self.token_false_id, self.token_true_id]]
                batch_scores = torch.nn.functional.log_softmax(batch_scores, dim=1)
                batch_log_probs = batch_scores[:, 1].tolist()
            for doc, score in zip(batch.documents, batch_log_probs):
                doc.score = score

        return texts


class DuoT5(Reranker):
    def __init__(self,
                 model: T5ForConditionalGeneration = None,
                 tokenizer: QueryDocumentBatchTokenizer = None,
                 use_amp = False):
        self.model = model or self.get_model()
        self.tokenizer = tokenizer or self.get_tokenizer()
        self.device = next(self.model.parameters(), None).device
        self.use_amp = use_amp

    @staticmethod
    def get_model(pretrained_model_name_or_path: str = 'castorini/duot5-base-msmarco',
                  *args, device: str = None, **kwargs) -> T5ForConditionalGeneration:
        device = device or ('cuda' if torch.cuda.is_available() else 'cpu')
        device = torch.device(device)
        return T5ForConditionalGeneration.from_pretrained(pretrained_model_name_or_path,
                                                          *args, **kwargs).to(device).eval()

    @staticmethod
    def get_tokenizer(pretrained_model_name_or_path: str = 't5-base',
                      *args, batch_size: int = 8, **kwargs) -> T5DuoBatchTokenizer:
        return T5DuoBatchTokenizer(
            AutoTokenizer.from_pretrained(pretrained_model_name_or_path, use_fast=False, *args, **kwargs),
            batch_size=batch_size
        )

    def rescore(self, query: Query, texts: List[Text]) -> List[Text]:
        texts = deepcopy(texts)
        doc_pairs = list(permutations(texts, 2))
        scores = defaultdict(float)
        batch_input = DuoQueryDocumentBatch(query=query, doc_pairs=doc_pairs)
        for batch in self.tokenizer.traverse_duo_query_document(batch_input):
            with torch.cuda.amp.autocast(enabled=self.use_amp):
                input_ids = batch.output['input_ids'].to(self.device)
                attn_mask = batch.output['attention_mask'].to(self.device)
                _, batch_scores = greedy_decode(self.model,
                                                input_ids,
                                                length=1,
                                                attention_mask=attn_mask,
                                                return_last_logits=True)

                # 6136 and 1176 are the indexes of the tokens false and true in T5.
                batch_scores = batch_scores[:, [6136, 1176]]
                batch_scores = torch.nn.functional.softmax(batch_scores, dim=1)
                batch_probs = batch_scores[:, 1].tolist()
            for doc, score in zip(batch.doc_pairs, batch_probs):
                scores[doc[0].metadata['docid']] += score
                scores[doc[1].metadata['docid']] += (1 - score)

        for text in texts:
            text.score = scores[text.metadata['docid']]

        return texts


class UnsupervisedTransformerReranker(Reranker):
    methods = dict(max=lambda x: x.max().item(),
                   mean=lambda x: x.mean().item(),
                   absmean=lambda x: x.abs().mean().item(),
                   absmax=lambda x: x.abs().max().item())

    def __init__(self,
                 model: PreTrainedModel,
                 tokenizer: BatchTokenizer,
                 sim_matrix_provider: SimilarityMatrixProvider,
                 method: str = 'max',
                 clean_special: bool = True,
                 argmax_only: bool = False):
        assert method in self.methods, 'inappropriate scoring method'
        self.model = model
        self.tokenizer = tokenizer
        self.encoder = LongBatchEncoder(model, tokenizer)
        self.sim_matrix_provider = sim_matrix_provider
        self.method = method
        self.clean_special = clean_special
        self.cleaner = SpecialTokensCleaner(tokenizer.tokenizer)
        self.device = next(self.model.parameters(), None).device
        self.argmax_only = argmax_only

    @torch.no_grad()
    def rescore(self, query: Query, texts: List[Text]) -> List[Text]:
        encoded_query = self.encoder.encode_single(query)
        encoded_documents = self.encoder.encode(texts)
        texts = deepcopy(texts)
        max_score = None
        for enc_doc, text in zip(encoded_documents, texts):
            if self.clean_special:
                enc_doc = self.cleaner.clean(enc_doc)
            matrix = self.sim_matrix_provider.compute_matrix(encoded_query,
                                                             enc_doc)
            score = self.methods[self.method](matrix) if matrix.size(1) > 0 \
                else -10000
            text.score = score
            max_score = score if max_score is None else max(max_score, score)
        if self.argmax_only:
            for text in texts:
                if text.score != max_score:
                    text.score = max_score - 10000

        return texts


class MonoBERT(Reranker):
    def __init__(self,
                 model: PreTrainedModel = None,
                 tokenizer: PreTrainedTokenizer = None,
                 use_amp = False):
        self.model = model or self.get_model()
        self.tokenizer = tokenizer or self.get_tokenizer()
        self.device = next(self.model.parameters(), None).device
        self.use_amp = use_amp

    @staticmethod
    def get_model(pretrained_model_name_or_path: str = 'castorini/monobert-large-msmarco',
                  *args, device: str = None, **kwargs) -> AutoModelForSequenceClassification:
        device = device or ('cuda' if torch.cuda.is_available() else 'cpu')
        device = torch.device(device)
        return AutoModelForSequenceClassification.from_pretrained(pretrained_model_name_or_path,
                                                                  *args, **kwargs).to(device).eval()

    @staticmethod
    def get_tokenizer(pretrained_model_name_or_path: str = 'bert-large-uncased',
                      *args, **kwargs) -> AutoTokenizer:
        return AutoTokenizer.from_pretrained(pretrained_model_name_or_path, use_fast=False, *args, **kwargs)

    @torch.no_grad()
    def rescore(self, query: Query, texts: List[Text]) -> List[Text]:
        texts = deepcopy(texts)
        for text in texts:
            ret = self.tokenizer.encode_plus(query.text,
                                             text.text,
                                             max_length=512,
                                             truncation=True,
                                             return_token_type_ids=True,
                                             return_tensors='pt')
            with torch.cuda.amp.autocast(enabled=self.use_amp):
                input_ids = ret['input_ids'].to(self.device)
                tt_ids = ret['token_type_ids'].to(self.device)
                output, = self.model(input_ids, token_type_ids=tt_ids, return_dict=False)
                if output.size(1) > 1:
                    text.score = torch.nn.functional.log_softmax(
                        output, 1)[0, -1].item()
                else:
                    text.score = output.item()

        return texts


class QuestionAnsweringTransformerReranker(Reranker):
    def __init__(self, model: PreTrainedModel, tokenizer: PreTrainedTokenizer):
        self.tokenizer = tokenizer
        self.model = model
        self.device = next(model.parameters()).device

    @torch.no_grad()
    def rescore(self, query: Query, texts: List[Text]) -> List[Text]:
        texts = deepcopy(texts)
        for text in texts:
            ret = self.tokenizer.encode_plus(query.text,
                                             text.text,
                                             max_length=512,
                                             truncation=True,
                                             return_tensors='pt',
                                             return_token_type_ids=True)
            input_ids = ret['input_ids'].to(self.device)
            tt_ids = ret['token_type_ids'].to(self.device)
            start_scores, end_scores = self.model(input_ids,
                                                  token_type_ids=tt_ids,
                                                  return_dict=False)
            start_scores = start_scores[0]
            end_scores = end_scores[0]
            start_scores[(1 - tt_ids[0]).bool()] = -5000
            end_scores[(1 - tt_ids[0]).bool()] = -5000
            smax_val, smax_idx = start_scores.max(0)
            emax_val, emax_idx = end_scores.max(0)
            text.score = max(smax_val.item(), emax_val.item())

        return texts


class SentenceTransformersReranker(Reranker):
    def __init__(self,
                 pretrained_model_name_or_path='cross-encoder/ms-marco-MiniLM-L-2-v2',
                 max_length=512,
                 device=None,
                 use_amp=False,
                 batch_size=32):
        device = device or ('cuda' if torch.cuda.is_available() else 'cpu')
        self.use_amp = use_amp
        self.model = CrossEncoder(
            pretrained_model_name_or_path, max_length=max_length, device=device
        )
        self.batch_size = batch_size

    def rescore(self, query: Query, texts: List[Text]) -> List[Text]:
        texts = deepcopy(texts)
        with torch.cuda.amp.autocast(enabled=self.use_amp):
            scores = self.model.predict(
                [(query.text, text.text) for text in texts],
                show_progress_bar=False,
                batch_size=self.batch_size,
            )

        for (text, score) in zip(texts, scores):
            text.score = score.item()

        return texts
        
