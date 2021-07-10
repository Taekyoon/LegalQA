from inspect import Parameter
import os
import re
import json
from typing import Dict, Optional, List, Iterable, Union, Tuple

import numpy as np
import torch
import pytorch_lightning as pl
from transformers import BartModel

from jina import Executor, DocumentArray, requests, Document, Flow
from jina.types.arrays.memmap import DocumentArrayMemmap
from jina.logging.logger import JinaLogger
from jina_commons.batching import get_docs_batch_generator

from kobart import get_kobart_tokenizer, get_pytorch_kobart_model


class PoolingHead(torch.nn.Module):
    def __init__(
        self,
        input_dim: int,
        inner_dim: int,
        pooler_dropout: float,
    ):
        super().__init__()
        self.dense = torch.nn.Linear(input_dim, inner_dim)
        self.dropout = torch.nn.Dropout(p=pooler_dropout)

    def forward(self, hidden_states: torch.Tensor):
        hidden_states = self.dropout(hidden_states)
        hidden_states = self.dense(hidden_states)
        return hidden_states


class KoBARTRegression(pl.LightningModule):
    def __init__(self, hparams, **kwargs):
        super(KoBARTRegression, self).__init__(**kwargs)
        self.save_hyperparameters(hparams)
        self.model = BartModel.from_pretrained(get_pytorch_kobart_model())
        self.pooling = PoolingHead(input_dim=self.model.config.d_model,
                                   inner_dim=self.model.config.d_model,
                                   pooler_dropout=0.1)
        self.classification = torch.nn.Linear(self.model.config.d_model * 3, 1)

    def forward(self, input_ids, attention_mask):
        return self.model(input_ids=input_ids,
                          attention_mask=attention_mask,
                          return_dict=True)

    def _get_encoding(self, input_ids, attention_mask, typ='norm_avg'):
        if typ == 'norm_avg':
            outs = self(input_ids, attention_mask)
            lengths = attention_mask.sum(dim=1)
            mask_3d = attention_mask.unsqueeze(dim=-1).repeat_interleave(
                repeats=self.model.config.d_model, dim=2)

            masked_encoder_out = (outs['last_hidden_state'] *
                                  mask_3d).sum(dim=1)
            # to avoid 0 division
            norm_encoder_out = masked_encoder_out / (lengths +
                                                     1).unsqueeze(dim=1)
            return norm_encoder_out
        elif typ == 'avg':
            last_hidden = self(input_ids, attention_mask)['last_hidden_state']
            return torch.mean(last_hidden, dim=1)

    def encoding(self, input_ids, attention_mask):
        return self.pooling(
            self._get_encoding(input_ids,
                               attention_mask,
                               typ=self.hparams.avg_type))


class KoSentenceBART(Executor):
    def __init__(
        self,
        pretrained_model_path: str = 'model/SentenceKoBART.bin',
        max_length: int = 128,
        device: str = 'cpu',
        default_traversal_paths: Optional[List[str]] = None,
        default_batch_size: int = 32,
        *args,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        if default_traversal_paths is not None:
            self.default_traversal_paths = default_traversal_paths
        else:
            self.default_traversal_paths = ['r']
        self.default_batch_size = default_batch_size
        self.pretrained_model_path = pretrained_model_path
        self.max_length = max_length

        self.logger = JinaLogger(self.__class__.__name__)
        if not device in ['cpu', 'cuda']:
            self.logger.error(
                'Torch device not supported. Must be cpu or cuda!')
            raise RuntimeError(
                'Torch device not supported. Must be cpu or cuda!')
        if device == 'cuda' and not torch.cuda.is_available():
            self.logger.warning(
                'You tried to use GPU but torch did not detect your'
                'GPU correctly. Defaulting to CPU. Check your CUDA installation!'
            )
            device = 'cpu'
        self.device = device
        self.tokenizer = get_kobart_tokenizer()
        self.model = KoBARTRegression.load_from_checkpoint(
            self.pretrained_model_path, hparams={'avg_type': 'norm_avg'})
        self.model.eval()
        self.model.to(torch.device(device))

    @requests
    def encode(self, docs: Optional[DocumentArray], parameters: Dict,
               **kwargs):
        """
        Encode text data into a ndarray of `D` as dimension, and fill the embedding of each Document.
        :param docs: DocumentArray containing text
        :param parameters: dictionary to define the `traversal_paths` and the `batch_size`. For example,
               `parameters={'traversal_paths': ['r'], 'batch_size': 10}`.
        :param kwargs: Additional key value arguments.
        """
        for batch in get_docs_batch_generator(
                docs,
                traversal_path=parameters.get('traversal_paths',
                                              self.default_traversal_paths),
                batch_size=parameters.get('batch_size',
                                          self.default_batch_size),
                needs_attr='text',
        ):
            texts = batch.get_attributes('text')
            processed_content = []
            for cont in texts:
                processed_content.append(self.tokenizer.bos_token + cont +
                                         self.tokenizer.eos_token)
            input_tensors = self.tokenizer(list(processed_content),
                                           return_tensors='pt',
                                           max_length=self.max_length,
                                           padding=True)
            with torch.no_grad():
                if self.device == 'cuda':
                    embedding = self.model.encoding(
                        input_tensors['input_ids'].cuda(),
                        input_tensors['attention_mask'].cuda())
                elif self.device == 'cpu':
                    embedding = self.model.encoding(
                        input_tensors['input_ids'],
                        input_tensors['attention_mask'])
                else:
                    assert False
                for doc, embed in zip(batch, embedding):
                    doc.embedding = embed.cpu().detach().numpy()


class Segmenter(Executor):
    def __init__(
            self,
            min_sent_len: int = 1,
            max_sent_len: int = 512,
            punct_chars: Optional[List[str]] = None,
            uniform_weight: bool = True,
            *args,
            **kwargs):
        super().__init__(*args, **kwargs)
        self.min_sent_len = min_sent_len
        self.max_sent_len = max_sent_len
        self.punct_chars = punct_chars
        self.uniform_weight = uniform_weight
        if not punct_chars:
            self.punct_chars = [
                '!', '.', '?', '։', '؟', '۔', '܀', '܁', '܂', '‼', '‽', '⁇',
                '⁈', '⁉', '⸮', '﹖', '﹗', '！', '．', '？', '｡', '。', '\n'
            ]
        if self.min_sent_len > self.max_sent_len:
            self.logger.warning(
                'the min_sent_len (={}) should be smaller or equal to the max_sent_len (={})'
                .format(self.min_sent_len, self.max_sent_len))
        self._slit_pat = re.compile('\s*([^{0}]+)(?<!\s)[{0}]*'.format(''.join(
            set(self.punct_chars))))

    def _split(self, text: str) -> List:
        results = []
        ret = [(m.group(0), m.start(), m.end())
               for m in re.finditer(self._slit_pat, text)]
        if not ret:
            ret = [(text, 0, len(text))]
        for ci, (r, s, e) in enumerate(ret):
            f = re.sub('\n+', ' ', r).strip()
            f = f[:self.max_sent_len]
            if len(f) > self.min_sent_len:
                results.append(
                    dict(text=f,
                         offset=ci,
                         weight=1.0 if self.uniform_weight else len(f) /
                         len(text),
                         location=[s, e]))
        return results

    @requests
    def segment(self, docs: DocumentArray, **kwargs):
        for doc in docs:
            text = doc.text
            chunks = self._split(text)
            for c in chunks:
                doc.chunks += [(Document(**c, mime_type='text/plain', tags={'root_doc_id': doc.id}))]


class DocVectorIndexer(Executor):
    def __init__(self, index_file_name: str, **kwargs):
        super().__init__(**kwargs)
        self._docs = DocumentArrayMemmap(self.workspace +
                                         f'/{index_file_name}')

    @requests(on='/index')
    def index(self, docs: DocumentArray, **kwargs):
        self._docs.extend(docs)

    @requests(on='/search')
    def search(self, docs: DocumentArray, parameters: Dict, **kwargs):
        if docs is None:
            return
        a = np.stack(docs.get_attributes('embedding'))
        b = np.stack(self._docs.get_attributes('embedding'))
        q_emb = _ext_A(_norm(a))
        d_emb = _ext_B(_norm(b))
        dists = _cosine(q_emb, d_emb)
        idx, dist = self._get_sorted_top_k(dists, int(parameters['top_k']))
        for _q, _ids, _dists in zip(docs, idx, dist):
            for _id, _dist in zip(_ids, _dists):
                d = Document(self._docs[int(_id)], copy=True)
                d.scores['cosine'] = 1 - _dist
                _q.matches.append(d)

    @staticmethod
    def _get_sorted_top_k(dist: 'np.array',
                          top_k: int) -> Tuple['np.ndarray', 'np.ndarray']:
        if top_k >= dist.shape[1]:
            idx = dist.argsort(axis=1)[:, :top_k]
            dist = np.take_along_axis(dist, idx, axis=1)
        else:
            idx_ps = dist.argpartition(kth=top_k, axis=1)[:, :top_k]
            dist = np.take_along_axis(dist, idx_ps, axis=1)
            idx_fs = dist.argsort(axis=1)
            idx = np.take_along_axis(idx_ps, idx_fs, axis=1)
            dist = np.take_along_axis(dist, idx_fs, axis=1)

        return idx, dist


class KeyValueIndexer(Executor):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._docs = DocumentArrayMemmap(self.workspace + '/kv-idx')

    @requests(on='/index')
    def index(self, docs: DocumentArray, **kwargs):
        self._docs.extend(docs)

    @requests(on='/search')
    def query(self, docs: DocumentArray, **kwargs):
        for doc in docs:
            for match in doc.matches:
                extracted_doc = self._docs[match.parent_id]
                match.update(extracted_doc)



class WeightedRanker(Executor):
    @requests(on='/search')
    def rank(
        self, docs_matrix: List['DocumentArray'], parameters: Dict, **kwargs
    ) -> 'DocumentArray':
        """
        :param docs_matrix: list of :class:`DocumentArray` on multiple requests to
          get bubbled up matches.
        :param parameters: the parameters passed into the ranker, in this case stores :attr`top_k`
          to filter k results based on score.
        :param kwargs: not used (kept to maintain interface)
        """

        result_da = DocumentArray()  # length: 1 as every time there is only one query
        for d_mod1, d_mod2 in zip(*docs_matrix):

            final_matches = {}  # type: Dict[str, Document]

            for m in d_mod1.matches:
                m.scores['relevance'] = m.scores['cosine'].value * d_mod1.weight
                final_matches[m.parent_id] = Document(m, copy=True)

            for m in d_mod2.matches:
                if m.parent_id in final_matches:
                    final_matches[m.parent_id].scores['relevance'] = final_matches[
                        m.parent_id
                    ].scores['relevance'].value + (
                        m.scores['cosine'].value * d_mod2.weight
                    )
                else:
                    m.scores['relevance'] = m.scores['cosine'].value * d_mod2.weight
                    final_matches[m.parent_id] = Document(m, copy=True)

            da = DocumentArray(list(final_matches.values()))
            da.sort(key=lambda ma: ma.scores['relevance'].value, reverse=True)
            d = Document(matches=da[: int(parameters['top_k'])])
            result_da.append(d)
        return result_da



def _get_ones(x, y):
    return np.ones((x, y))


def _ext_A(A):
    nA, dim = A.shape
    A_ext = _get_ones(nA, dim * 3)
    A_ext[:, dim:2 * dim] = A
    A_ext[:, 2 * dim:] = A**2
    return A_ext


def _ext_B(B):
    nB, dim = B.shape
    B_ext = _get_ones(dim * 3, nB)
    B_ext[:dim] = (B**2).T
    B_ext[dim:2 * dim] = -2.0 * B.T
    del B
    return B_ext


def _euclidean(A_ext, B_ext):
    sqdist = A_ext.dot(B_ext).clip(min=0)
    return np.sqrt(sqdist)


def _norm(A):
    return A / np.linalg.norm(A, ord=2, axis=1, keepdims=True)


def _cosine(A_norm_ext, B_norm_ext):
    return A_norm_ext.dot(B_norm_ext).clip(min=0) / 2


def _pre_processing(texts):
    print('start of pre-processing')
    results = []
    for i in texts:
        d = json.loads(i)
        d['text'] = d['title'].strip() + '. ' + d['question']
        results.append(Document(json.dumps(d, ensure_ascii=False)))
    return results



def print_topk(resp, sentence):
    for doc in resp.data.docs:
        print(f"\n\n\nTa-Dah🔮, here's what we found for: {sentence}")
        for idx, match in enumerate(doc.matches):
            score = match.scores['cosine'].value
            if score < 0.0:
                continue
            print(f'> {idx:>2d}({score:.2f}). {match.text}')
        print('\n\n\n')

if __name__ == '__main__':
    f = Flow().add(name='segment', uses={'jtype': 'Segmenter', 'with': {'punct_chars': '!?.'}}).add(uses={'jtype': 'KoSentenceBART', 'with': {'default_traversal_paths': ['r', 'c']}}).add(name='docvec', uses={'jtype': 'DocVectorIndexer', 'with': {'index_file_name': 'test.idx'}}, workspace='spaces').add(name='keyval',uses={'jtype': 'KeyValueIndexer'}, needs='segment', workspace='spaces').needs(['docvec', 'keyval'], name='joinAll')
    data_path = os.path.join(os.path.dirname(__file__),
                             os.environ.get('JINA_DATA_FILE', None))
    # with f:
    #     a = f.post('/index',
    #                _pre_processing(open(data_path, 'rt').readlines()),
    #                request_size=10,
    #                show_progress=True,
    #                return_results=True)

    # f = Flow().add(name='segment', uses={'jtype': 'Segmenter', 'with': {'punct_chars': '!?.'}}).add(uses={'jtype': 'KoSentenceBART', 'with': {'default_traversal_paths': ['c']}}).add(name='docvec', uses={'jtype': 'DocVectorIndexer', 'with': {'index_file_name': 'test.idx'}}, workspace='spaces').add(name='keyval',uses={'jtype': 'KeyValueIndexer'}, needs='docvec', workspace='spaces')
    with f:
        while True:
            text = input("Please type a sentence: ")
            if not text:
                break

            def ppr(x):
                print_topk(x, text)

            f.search(Document(text=text), parameters={'top_k': 5}, on_done=ppr)
            