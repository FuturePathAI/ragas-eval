from __future__ import annotations

import logging
import typing as t
from dataclasses import dataclass
from random import choices
import pickle
import numpy as np

import pandas as pd
from datasets import Dataset
from langchain_openai.chat_models import ChatOpenAI
from langchain_openai.embeddings import OpenAIEmbeddings

from ragas._analytics import TesetGenerationEvent, track
from ragas.embeddings.base import BaseRagasEmbeddings, LangchainEmbeddingsWrapper
from ragas.exceptions import ExceptionInRunner
from ragas.executor import Executor
from ragas.llms import BaseRagasLLM, LangchainLLMWrapper
from ragas.run_config import RunConfig
from ragas.testset.docstore import Document, DocumentStore, InMemoryDocumentStore
from ragas.testset.evolutions import (
    ComplexEvolution,
    CurrentNodes,
    DataRow,
    Evolution,
    multi_context,
    reasoning,
    simple,
)
from ragas.testset.extractor import KeyphraseExtractor
from ragas.testset.filters import EvolutionFilter, NodeFilter, QuestionFilter
from ragas.utils import check_if_sum_is_close, get_feature_language, is_nan

if t.TYPE_CHECKING:
    from langchain_core.documents import Document as LCDocument
    from llama_index.core.schema import Document as LlamaindexDocument

logger = logging.getLogger(__name__)

Distributions = t.Dict[t.Any, float]
DEFAULT_DISTRIBUTION = {simple: 0.5, reasoning: 0.25, multi_context: 0.25}


# from langchain.embeddings import AzureOpenAIEmbeddings
# from langchain.chat_models import AzureChatOpenAI
from langchain_openai import AzureChatOpenAI
from langchain_openai import AzureOpenAIEmbeddings

def get_model(host="azure"):
    if host == 'openai':
        model_def = ChatOpenAI(model='gpt-4')
        embeddings_def = OpenAIEmbeddings(model="text-embedding-ada-002")

    else:
        model_def = AzureChatOpenAI(
                api_key='0af33f2fcade4c6ab6120299c61e9940',
                api_version='2023-05-15',
                deployment_name='chat',
                model='gpt-4',
                azure_endpoint='https://cog-2o7x3ehmzkvdg.openai.azure.com//',
                openai_api_type='azure',
            )
        embeddings_def = AzureOpenAIEmbeddings(
                api_key='0af33f2fcade4c6ab6120299c61e9940',
                api_version='2023-05-15',
                deployment='embedding',
                model='text-embedding-ada-002',
                azure_endpoint='https://cog-2o7x3ehmzkvdg.openai.azure.com/',
                openai_api_type='azure',
            )
    return model_def, embeddings_def
        

@dataclass
class TestDataset:
    """
    TestDataset class
    """

    test_data: t.List[DataRow]

    def _to_records(self) -> t.List[t.Dict]:
        data_samples = []
        for data in self.test_data:
            data_dict = dict(data)
            data_dict["episode_done"] = True
            data_samples.append(data_dict)
        return data_samples

    def to_pandas(self) -> pd.DataFrame:
        return pd.DataFrame.from_records(self._to_records())

    def to_dataset(self) -> Dataset:
        return Dataset.from_list(self._to_records())


@dataclass
class TestsetGenerator:
    generator_llm: BaseRagasLLM
    critic_llm: BaseRagasLLM
    embeddings: BaseRagasEmbeddings
    docstore: DocumentStore

    @classmethod
    def with_openai(
        cls,
        generator_llm: str = "gpt-3.5-turbo-16k",
        critic_llm: str = "gpt-4",
        embeddings: str = "text-embedding-ada-002",
        docstore: t.Optional[DocumentStore] = None,
        chunk_size: int = 512,
        host: str = 'azure'
    ) -> "TestsetGenerator":
        print(f"Using {host}")
        model_def, embeddings_def = get_model(host)
        generator_llm_model = LangchainLLMWrapper(model_def)
        critic_llm_model = LangchainLLMWrapper(model_def)
        embeddings_model = LangchainEmbeddingsWrapper(embeddings_def)
        keyphrase_extractor = KeyphraseExtractor(llm=generator_llm_model)
        # print("done")
        if docstore is None:
            # print("done1.1")
            from langchain.text_splitter import TokenTextSplitter
            # print("done1.3")
            splitter = TokenTextSplitter(chunk_size=chunk_size, chunk_overlap=0)
            # print("done1.5")
            docstore = InMemoryDocumentStore(
                splitter=splitter,
                embeddings=embeddings_model,
                extractor=keyphrase_extractor,
            )
            # print("done2")
            return cls(
                generator_llm=generator_llm_model,
                critic_llm=critic_llm_model,
                embeddings=embeddings_model,
                docstore=docstore,
            )
        else:
            return cls(
                generator_llm=generator_llm_model,
                critic_llm=critic_llm_model,
                embeddings=embeddings_model,
                docstore=docstore,
            )

    # if you add any arguments to this function, make sure to add them to
    # generate_with_langchain_docs as well
    def generate_with_llamaindex_docs(
        self,
        documents: t.Sequence[LlamaindexDocument],
        test_size: int,
        distributions: Distributions = {},
        with_debugging_logs=False,
        is_async: bool = True,
        raise_exceptions: bool = True,
        run_config: t.Optional[RunConfig] = None,
    ):
        # chunk documents and add to docstore
        self.docstore.add_documents(
            [Document.from_llamaindex_document(doc) for doc in documents]
        )

        return self.generate(
            test_size=test_size,
            distributions=distributions,
            with_debugging_logs=with_debugging_logs,
            is_async=is_async,
            run_config=run_config,
            raise_exceptions=raise_exceptions,
        )

    # if you add any arguments to this function, make sure to add them to
    # generate_with_langchain_docs as well

    def create_node_embeddings(
        self, 
        documents: t.Sequence[LCDocument], 
        save_path
    ):
        self.docstore.add_documents(
            [Document.from_langchain_document(doc) for doc in documents]
        )
        with open(save_path, 'wb') as file:
            pickle.dump((self.docstore.nodes, self.docstore.node_map, self.docstore.node_embeddings_list), file)

    def load_saved_embeddings(
            self, 
            save_path: str
    ):
        with open(save_path, 'rb') as file:
            self.docstore.nodes, self.docstore.node_map, self.docstore.node_embeddings_list = pickle.load(file)

    def load_node_scoring(self, save_path: str):
        with open(save_path, 'rb') as file:
            self.docstore.node_scores = pickle.load(file)

    def generate_with_langchain_docs(
        self,
        documents: t.Sequence[LCDocument],
        test_size: int,
        distributions: Distributions = {},
        with_debugging_logs=False,
        is_async: bool = True,
        raise_exceptions: bool = True,
        run_config: t.Optional[RunConfig] = None,
    ):
        # chunk documents and add to docstore
        self.docstore.add_documents(
            [Document.from_langchain_document(doc) for doc in documents]
        )

        return self.generate(
            test_size=test_size,
            distributions=distributions,
            with_debugging_logs=with_debugging_logs,
            is_async=is_async,
            raise_exceptions=raise_exceptions,
            run_config=run_config,
        )

    def generate_with_saved_embeddings(
        self,
        save_path: str,
        test_size: int,
        distributions: Distributions = {},
        with_debugging_logs=False,
        is_async: bool = True,
        raise_exceptions: bool = True,
        run_config: t.Optional[RunConfig] = None,
    ):
        with open(save_path, 'rb') as file:
            self.docstore.nodes, self.docstore.node_map, self.docstore.node_embeddings_list = pickle.load(file)

        return self.generate(
            test_size=test_size,
            distributions=distributions,
            with_debugging_logs=with_debugging_logs,
            is_async=is_async,
            raise_exceptions=raise_exceptions,
            run_config=run_config,
        )
    
    async def filter_nodes_test(self, evolution, node=None):
        self.evolution = evolution
        self.init_evolution(self.evolution)

        # self.passed_nodes = []
        # save_path = f"/home/nithin/fp/ai-rag-chat-evaluator/scripts/data-generator/nodes.pkl"

        # for i in [75, 84, 88,75, 84, 88,75, 84, 88,75, 84, 88]:
        if node == None:
            for i in range(200):
                n = self.docstore.nodes[i]
                passed = await self.evolution.node_filter.filter(n)
                print(i, passed['score'])
        else:
            passed = await self.evolution.node_filter.filter(node)
            # print(passed['score'])
            return passed['score']
            # if passed['score']:
            #     self.passed_nodes.append(i)
            #     with open(save_path, 'wb') as file:
            #         pickle.dump(self.passed_nodes, file)

    def init_evolution(self, evolution: Evolution) -> None:
        if evolution.generator_llm is None:
            evolution.generator_llm = self.generator_llm
            if evolution.docstore is None:
                evolution.docstore = self.docstore

            if evolution.question_filter is None:
                evolution.question_filter = QuestionFilter(llm=self.critic_llm)
            if evolution.node_filter is None:
                evolution.node_filter = NodeFilter(llm=self.critic_llm)

            if isinstance(evolution, ComplexEvolution):
                if evolution.evolution_filter is None:
                    evolution.evolution_filter = EvolutionFilter(llm=self.critic_llm)

    def generate(
        self,
        test_size: int,
        distributions: Distributions = DEFAULT_DISTRIBUTION,
        with_debugging_logs=False,
        is_async: bool = True,
        raise_exceptions: bool = True,
        run_config: t.Optional[RunConfig] = None,
    ):
        # validate distributions
        if not check_if_sum_is_close(list(distributions.values()), 1.0, 3):
            raise ValueError(
                f"distributions passed do not sum to 1.0 [got {sum(list(distributions.values()))}]. Please check the distributions."
            )

        # configure run_config for docstore
        if run_config is None:
            run_config = RunConfig(max_retries=15, max_wait=600)
        self.docstore.set_run_config(run_config)

        # init filters and evolutions
        for evolution in distributions:
            self.init_evolution(evolution)
            evolution.init(is_async=is_async, run_config=run_config)

        if with_debugging_logs:
            from ragas.utils import patch_logger

            patch_logger("ragas.testset.evolutions", logging.DEBUG)
            patch_logger("ragas.testset.extractor", logging.DEBUG)
            patch_logger("ragas.testset.filters", logging.DEBUG)
            patch_logger("ragas.testset.docstore", logging.DEBUG)
            patch_logger("ragas.llms.prompt", logging.DEBUG)

        execs = [Executor(
                    desc="Generating_0",
                    keep_progress_bar=True,
                    raise_exceptions=raise_exceptions,
                )]
        max_parallel_process = 10

        current_nodes = [
            CurrentNodes(root_node=n, nodes=[n])
            for n in self.docstore.get_random_nodes(k=test_size)
        ]
        total_evolutions = 0
        for evolution, probability in distributions.items():
            for i in range(round(probability * test_size)):
                execs[-1].submit(
                    evolution.evolve,
                    current_nodes[i],
                    name=f"{evolution.__class__.__name__}-{i}",
                )
                if total_evolutions % max_parallel_process == max_parallel_process - 1:
                    execs.append(
                        Executor(
                            desc=f"Generating_{len(execs)}",
                            keep_progress_bar=True,
                            raise_exceptions=raise_exceptions,
                        )
                    )
                total_evolutions += 1
        if total_evolutions <= test_size:
            filler_evolutions = choices(
                list(distributions), k=test_size - total_evolutions
            )
            for evolution in filler_evolutions:
                execs[-1].submit(
                    evolution.evolve,
                    current_nodes[total_evolutions],
                    name=f"{evolution.__class__.__name__}-{total_evolutions}",
                )
                if total_evolutions % max_parallel_process == max_parallel_process - 1:
                    execs.append(
                        Executor(
                            desc=f"Generating_{len(execs)}",
                            keep_progress_bar=True,
                            raise_exceptions=raise_exceptions,
                        )
                    )
                total_evolutions += 1

        try:
            total_test_data_rows = []
            print("Total generation workflows: ", len(execs))
            for exec in execs:
                test_data_rows = exec.results()
                total_test_data_rows += test_data_rows
            if total_test_data_rows == []:
                raise ExceptionInRunner()

        except ValueError as e:
            raise e
        # make sure to ignore any NaNs that might have been returned
        # due to failed evolutions. MaxRetriesExceeded is a common reason
        total_test_data_rows = [r for r in total_test_data_rows if not is_nan(r)]
        test_dataset = TestDataset(test_data=total_test_data_rows)
        evol_lang = [get_feature_language(e) for e in distributions]
        evol_lang = [e for e in evol_lang if e is not None]
        track(
            TesetGenerationEvent(
                event_type="testset_generation",
                evolution_names=[e.__class__.__name__.lower() for e in distributions],
                evolution_percentages=[distributions[e] for e in distributions],
                num_rows=len(test_dataset.test_data),
                language=evol_lang[0] if len(evol_lang) > 0 else "",
            )
        )

        return test_dataset
    
    async def generate_single(self, evolution: Evolution, score_threshold: float = 4.0, node_index: int = None):
        
        run_config = RunConfig(max_retries=15, max_wait=600)
        self.docstore.set_run_config(run_config)

        self.init_evolution(evolution)
        evolution.init(is_async=True, run_config=run_config, score_threshold=score_threshold)

        if node_index is not None:
            assert node_index < len(self.docstore.nodes)
            node = self.docstore.nodes[node_index]
        else:
            node = self.docstore.get_random_nodes(k=1, score_threshold=score_threshold)

        current_node = CurrentNodes(root_node=node, nodes=[node])

        try:
            test_data_row = await evolution.evolve(current_node)
        except Exception as e:
            raise e
        
        if not is_nan(test_data_row):
            return TestDataset(test_data=test_data_row)
        else:
            return None
    
    def adapt(
        self,
        language: str,
        evolutions: t.List[Evolution],
        cache_dir: t.Optional[str] = None,
    ) -> None:
        assert isinstance(
            self.docstore, InMemoryDocumentStore
        ), "Must be an instance of in-memory docstore"
        assert self.docstore.extractor is not None, "Extractor is not set"

        self.docstore.extractor.adapt(language, cache_dir=cache_dir)
        for evolution in evolutions:
            self.init_evolution(evolution)
            evolution.init()
            evolution.adapt(language, cache_dir=cache_dir)

    def save(
        self, evolutions: t.List[Evolution], cache_dir: t.Optional[str] = None
    ) -> None:
        """
        Save the docstore prompts to a path.
        """
        assert isinstance(
            self.docstore, InMemoryDocumentStore
        ), "Must be an instance of in-memory docstore"
        assert self.docstore.extractor is not None, "Extractor is not set"

        self.docstore.extractor.save(cache_dir)
        for evolution in evolutions:
            assert evolution.node_filter is not None, "NodeFilter is not set"
            assert evolution.question_filter is not None, "QuestionFilter is not set"
            if isinstance(evolution, ComplexEvolution):
                assert (
                    evolution.evolution_filter is not None
                ), "EvolutionFilter is not set"
            evolution.save(cache_dir=cache_dir)
