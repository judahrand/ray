import re
import time
from typing import Optional

import numpy as np
import pandas as pd
import pyarrow as pa

import pytest
from ray.air.constants import MAX_REPR_LENGTH, PREPROCESSOR_KEY

import ray
from ray.air.checkpoint import Checkpoint
from ray.air.util.data_batch_conversion import BatchFormat
from ray.data import Preprocessor
from ray.data.preprocessors import BatchMapper
from ray.tests.conftest import *  # noqa
from ray.train.batch_predictor import BatchPredictor
from ray.train.predictor import Predictor


class DummyPreprocessor(Preprocessor):
    _is_fittable = False

    def __init__(self, multiplier=2):
        self.multiplier = multiplier

    def _transform_pandas(self, df):
        return df * self.multiplier


class DummyWithNumpyPreprocessor(DummyPreprocessor):
    def _transform_numpy(self, np_data):
        if isinstance(np_data, dict):
            return {k: v * self.multiplier for k, v in np_data.items()}
        else:
            return np_data * self.multiplier


def test_repr(shutdown_only):
    predictor = BatchPredictor.from_checkpoint(
        Checkpoint.from_dict({"factor": 2.0}),
        DummyPredictorFS,
    )

    representation = repr(predictor)

    assert len(representation) < MAX_REPR_LENGTH
    pattern = re.compile("^BatchPredictor\\((.*)\\)$")
    assert pattern.match(representation)


class DummyPredictor(Predictor):
    def __init__(
        self,
        factor: float = 1.0,
        preprocessor: Optional[Preprocessor] = None,
        use_gpu: bool = False,
    ):
        self.factor = factor
        self.use_gpu = use_gpu
        super().__init__(preprocessor)

    @classmethod
    def from_checkpoint(
        cls, checkpoint: Checkpoint, use_gpu: bool = False, **kwargs
    ) -> "DummyPredictor":
        checkpoint_data = checkpoint.to_dict()
        preprocessor = checkpoint.get_preprocessor()
        return cls(
            checkpoint_data["factor"], preprocessor=preprocessor, use_gpu=use_gpu
        )

    def _predict_pandas(self, data: pd.DataFrame, **kwargs) -> pd.DataFrame:
        # Need to throw exception here instead of constructor to surface the
        # exception to pytest rather than ray worker.
        if self.use_gpu and "allow_gpu" not in kwargs:
            raise ValueError("DummyPredictor does not support GPU prediction.")
        else:
            return data * self.factor


class DummyWithNumpyPredictor(DummyPredictor):
    def _predict_numpy(self, data, **kwargs):
        if isinstance(data, dict):
            return {k: v * self.factor for k, v in data.items()}
        else:
            return data * self.factor

    @classmethod
    def preferred_batch_format(cls) -> BatchFormat:
        return BatchFormat.NUMPY


class DummyPredictorFS(DummyPredictor):
    @classmethod
    def from_checkpoint(cls, checkpoint: Checkpoint, **kwargs) -> "DummyPredictor":
        with checkpoint.as_directory():
            # simulate reading
            time.sleep(1)
        checkpoint_data = checkpoint.to_dict()
        preprocessor = checkpoint.get_preprocessor()
        return cls(checkpoint_data["factor"], preprocessor=preprocessor)


def test_separate_gpu_stage(shutdown_only):
    ray.init(num_gpus=1)
    batch_predictor = BatchPredictor.from_checkpoint(
        Checkpoint.from_dict({"factor": 2.0, PREPROCESSOR_KEY: DummyPreprocessor()}),
        DummyPredictor,
    )
    ds = batch_predictor.predict(
        ray.data.range(10),
        num_gpus_per_worker=1,
        separate_gpu_stage=True,
        allow_gpu=True,
    ).materialize()
    stats = ds.stats()
    assert "Stage 1 ReadRange->DummyPreprocessor:" in stats, stats
    assert "Stage 2 MapBatches(ScoringWrapper):" in stats, stats
    assert ds.max("id") == 36.0, ds

    ds = batch_predictor.predict(
        ray.data.range(10),
        num_gpus_per_worker=1,
        separate_gpu_stage=False,
        allow_gpu=True,
    ).materialize()
    stats = ds.stats()
    assert "Stage 1 ReadRange:" in stats, stats
    assert "Stage 2 MapBatches(ScoringWrapper):" in stats, stats
    assert ds.max("id") == 36.0, ds


def test_automatic_enable_gpu_from_num_gpus_per_worker(shutdown_only):
    """
    Test we automatically set underlying Predictor creation use_gpu to True if
    we found num_gpus_per_worker > 0 in BatchPredictor's predict() call.
    """
    ray.init(num_gpus=1)

    batch_predictor = BatchPredictor.from_checkpoint(
        Checkpoint.from_dict({"factor": 2.0, PREPROCESSOR_KEY: DummyPreprocessor()}),
        DummyPredictor,
    )
    test_dataset = ray.data.range(4)

    with pytest.raises(
        ValueError, match="DummyPredictor does not support GPU prediction"
    ):
        batch_predictor.predict(test_dataset, num_gpus_per_worker=1).materialize()


def test_batch_prediction():
    batch_predictor = BatchPredictor.from_checkpoint(
        Checkpoint.from_dict({"factor": 2.0, PREPROCESSOR_KEY: DummyPreprocessor()}),
        DummyPredictor,
    )

    test_dataset = ray.data.range(4)
    ds = batch_predictor.predict(test_dataset).materialize()
    # Check fusion occurred.
    assert "ReadRange->DummyPreprocessor" in ds.stats(), ds.stats()
    assert ds.to_pandas().to_numpy().squeeze().tolist() == [
        0.0,
        4.0,
        8.0,
        12.0,
    ]

    test_dataset = ray.data.range(4)
    assert next(
        batch_predictor.predict_pipelined(
            test_dataset, blocks_per_window=2
        ).iter_datasets()
    ).to_pandas().to_numpy().squeeze().tolist() == [
        0.0,
        4.0,
    ]


def test_batch_prediction_various_combination():
    """Dataset level predictor test against various
    - Dataset format
    - Preprocessor implementation (pandas vs numpy)
    - Predictor implementation (pandas vs numpy)
    """
    # Got to inline this rather than using @pytest.mark.parametrize to void
    # unknown object owner error when running test with python cli.
    test_cases = [
        (
            DummyPreprocessor(),
            DummyPredictor,
            ray.data.from_pandas(pd.DataFrame({"x": [1, 2, 3]})),
            # Pandas predictor outputs Pandas dataset.
            "pandas",
        ),
        (
            DummyWithNumpyPreprocessor(),
            DummyPredictor,
            ray.data.from_pandas(pd.DataFrame({"x": [1, 2, 3]})),
            # Pandas predictor outputs Pandas dataset.
            "pandas",
        ),
        (
            DummyPreprocessor(),
            DummyWithNumpyPredictor,
            ray.data.from_pandas(pd.DataFrame({"x": [1, 2, 3]})),
            # Numpy predictor outputs Arrow dataset.
            "arrow",
        ),
        (
            DummyWithNumpyPreprocessor(),
            DummyWithNumpyPredictor,
            ray.data.from_pandas(pd.DataFrame({"x": [1, 2, 3]})),
            # Numpy predictor outputs Arrow dataset.
            "arrow",
        ),
        (
            DummyPreprocessor(),
            DummyWithNumpyPredictor,
            ray.data.from_arrow(pa.Table.from_pydict({"x": [1, 2, 3]})),
            # Numpy predictor outputs Arrow dataset.
            "arrow",
        ),
        (
            DummyWithNumpyPreprocessor(),
            DummyWithNumpyPredictor,
            ray.data.from_arrow(pa.Table.from_pydict({"x": [1, 2, 3]})),
            # Numpy predictor outputs Arrow dataset.
            "arrow",
        ),
        (
            DummyPreprocessor(),
            DummyPredictor,
            ray.data.from_arrow(pa.Table.from_pydict({"x": [1, 2, 3]})),
            # Pandas predictor outputs Pandas dataset.
            "pandas",
        ),
        (
            DummyWithNumpyPreprocessor(),
            DummyPredictor,
            ray.data.from_arrow(pa.Table.from_pydict({"x": [1, 2, 3]})),
            # Pandas predictor outputs Pandas dataset.
            "pandas",
        ),
        (
            DummyPreprocessor(),
            DummyWithNumpyPredictor,
            ray.data.from_items([1, 2, 3]),
            # Numpy predictor outputs Arrow dataset.
            "arrow",
        ),
        (
            DummyWithNumpyPreprocessor(),
            DummyWithNumpyPredictor,
            ray.data.from_items([1, 2, 3]),
            # Numpy predictor outputs Arrow dataset.
            "arrow",
        ),
        (
            DummyPreprocessor(),
            DummyPredictor,
            ray.data.from_items([1, 2, 3]),
            # Pandas predictor outputs Pandas dataset.
            "pandas",
        ),
        (
            DummyWithNumpyPreprocessor(),
            DummyPredictor,
            ray.data.from_items([1, 2, 3]),
            # Pandas predictor outputs Pandas dataset.
            "pandas",
        ),
    ]

    for test_case in test_cases:
        preprocessor, predictor_cls, input_dataset, dataset_format = test_case
        # Test with pandas preprocessor
        batch_predictor = BatchPredictor.from_checkpoint(
            Checkpoint.from_dict({"factor": 2.0, PREPROCESSOR_KEY: preprocessor}),
            predictor_cls,
        )

        ds = batch_predictor.predict(input_dataset).materialize()
        # Check no fusion needed since we're not doing a dataset read.
        assert f"Stage 1 {preprocessor.__class__.__name__}" in ds.stats(), ds.stats()
        assert ds.to_pandas().to_numpy().squeeze().tolist() == [
            4.0,
            8.0,
            12.0,
        ]
        block = ray.get(ds.get_internal_block_refs()[0])
        if dataset_format == "pandas":
            assert isinstance(block, pd.DataFrame)
        elif dataset_format == "arrow":
            assert isinstance(block, pa.Table)
        else:
            raise ValueError(
                f"Unsupported test case with dataset format: {dataset_format}"
            )


def test_batch_prediction_fs():
    batch_predictor = BatchPredictor.from_checkpoint(
        Checkpoint.from_dict({"factor": 2.0, PREPROCESSOR_KEY: DummyPreprocessor()}),
        DummyPredictorFS,
    )

    test_dataset = ray.data.from_pandas(
        pd.DataFrame({"x": [1.0, 2.0, 3.0, 4.0] * 32})
    ).repartition(8)
    assert (
        batch_predictor.predict(test_dataset, min_scoring_workers=4)
        .to_pandas()
        .to_numpy()
        .squeeze()
        .tolist()
        == [
            4.0,
            8.0,
            12.0,
            16.0,
        ]
        * 32
    )


def test_batch_prediction_feature_cols():
    # Pandas path
    batch_predictor = BatchPredictor.from_checkpoint(
        Checkpoint.from_dict({"factor": 2.0, PREPROCESSOR_KEY: DummyPreprocessor()}),
        DummyPredictor,
    )

    test_dataset = ray.data.from_pandas(pd.DataFrame({"a": [1, 2, 3], "b": [4, 5, 6]}))

    assert batch_predictor.predict(
        test_dataset, feature_columns=["a"]
    ).to_pandas().to_numpy().squeeze().tolist() == [4.0, 8.0, 12.0]

    # Numpy path
    batch_predictor = BatchPredictor.from_checkpoint(
        Checkpoint.from_dict(
            {"factor": 2.0, PREPROCESSOR_KEY: DummyWithNumpyPreprocessor()}
        ),
        DummyWithNumpyPredictor,
    )

    test_dataset = ray.data.from_arrow(
        pa.Table.from_pydict({"a": [1, 2, 3], "b": [4, 5, 6]})
    )

    assert batch_predictor.predict(
        test_dataset, feature_columns=["a"]
    ).to_pandas().to_numpy().squeeze().tolist() == [4.0, 8.0, 12.0]


def test_batch_prediction_feature_cols_after_preprocess():
    """Tests that feature cols are applied after preprocessing the dataset."""

    # Pandas path
    class DummyPreprocessorWithColumn(DummyPreprocessor):
        def _transform_pandas(self, df):
            df["c"] = df["a"]
            return super()._transform_pandas(df)

    batch_predictor = BatchPredictor.from_checkpoint(
        Checkpoint.from_dict(
            {"factor": 2.0, PREPROCESSOR_KEY: DummyPreprocessorWithColumn()}
        ),
        DummyPredictor,
    )
    test_dataset = ray.data.from_pandas(pd.DataFrame({"a": [1, 2, 3], "b": [4, 5, 6]}))

    assert batch_predictor.predict(
        test_dataset, feature_columns=["c"]
    ).to_pandas().to_numpy().squeeze().tolist() == [4.0, 8.0, 12.0]

    # Numpy path
    class DummyNumpyPreprocessorWithColumn(DummyWithNumpyPreprocessor):
        def _transform_numpy(self, np_dict):
            np_dict["c"] = np.copy(np_dict["a"])
            return super()._transform_numpy(np_dict)

    batch_predictor = BatchPredictor.from_checkpoint(
        Checkpoint.from_dict(
            {"factor": 2.0, PREPROCESSOR_KEY: DummyNumpyPreprocessorWithColumn()}
        ),
        DummyWithNumpyPredictor,
    )

    test_dataset = ray.data.from_arrow(
        pa.Table.from_pydict({"a": [1, 2, 3], "b": [4, 5, 6]})
    )

    assert batch_predictor.predict(
        test_dataset, feature_columns=["a"]
    ).to_pandas().to_numpy().squeeze().tolist() == [4.0, 8.0, 12.0]


def test_batch_predictor_transform_config():
    """Tests that the preprocessor's transform config is
    respected when using BatchPredictor."""
    batch_size = 2

    def check_batch(batch):
        assert isinstance(batch, dict)
        assert isinstance(batch["id"], np.ndarray)
        assert len(batch["id"]) == batch_size
        return batch

    prep = BatchMapper(check_batch, batch_format="numpy", batch_size=2)
    ds = ray.data.range(6, parallelism=1)

    batch_predictor = BatchPredictor.from_checkpoint(
        Checkpoint.from_dict({"factor": 2.0, PREPROCESSOR_KEY: prep}),
        DummyWithNumpyPredictor,
    )

    batch_predictor.predict(ds)

    # Pipelined case.
    ds = ray.data.range(6, parallelism=1)

    batch_predictor.predict_pipelined(ds, blocks_per_window=1)


def test_batch_prediction_keep_cols():
    # Pandas path
    batch_predictor = BatchPredictor.from_checkpoint(
        Checkpoint.from_dict({"factor": 2.0, PREPROCESSOR_KEY: DummyPreprocessor()}),
        DummyPredictor,
    )

    test_dataset = ray.data.from_pandas(
        pd.DataFrame({"a": [1, 2, 3], "b": [4, 5, 6], "c": [7, 8, 9]})
    )

    output_df = batch_predictor.predict(
        test_dataset, feature_columns=["a"], keep_columns=["b"]
    ).to_pandas()

    assert set(output_df.columns) == {"a", "b"}

    assert output_df["a"].tolist() == [4.0, 8.0, 12.0]
    assert output_df["b"].tolist() == [8, 10, 12]

    # Numpy path
    batch_predictor = BatchPredictor.from_checkpoint(
        Checkpoint.from_dict(
            {"factor": 2.0, PREPROCESSOR_KEY: DummyWithNumpyPreprocessor()}
        ),
        DummyWithNumpyPredictor,
    )

    test_dataset = ray.data.from_arrow(
        pa.Table.from_pydict({"a": [1, 2, 3], "b": [4, 5, 6], "c": [7, 8, 9]})
    )

    output_df = batch_predictor.predict(
        test_dataset, feature_columns=["a"], keep_columns=["b"]
    ).to_pandas()

    assert set(output_df.columns) == {"a", "b"}

    assert output_df["a"].tolist() == [4.0, 8.0, 12.0]
    assert output_df["b"].tolist() == [8, 10, 12]


def test_batch_prediction_from_pandas_udf():
    def check_truth(df, all_true=False):
        if all_true:
            return pd.DataFrame({"bool": [True] * len(df)})
        return pd.DataFrame({"bool": df["a"] == df["b"]})

    batch_predictor = BatchPredictor.from_pandas_udf(check_truth)

    test_dataset = ray.data.from_pandas(pd.DataFrame({"a": [1, 2, 3], "b": [1, 5, 6]}))

    output_ds = batch_predictor.predict(test_dataset)
    output = [row["bool"] for row in output_ds.take()]
    assert output == [True, False, False]

    output_ds = batch_predictor.predict(test_dataset, all_true=True)
    output = [row["bool"] for row in output_ds.take()]
    assert output == [True, True, True]


def test_get_and_set_preprocessor():
    """Test preprocessor can be set and get."""

    preprocessor = DummyPreprocessor(1)
    batch_predictor = BatchPredictor.from_checkpoint(
        Checkpoint.from_dict({"factor": 2.0, PREPROCESSOR_KEY: preprocessor}),
        DummyPredictor,
    )
    assert batch_predictor.get_preprocessor() == preprocessor

    test_dataset = ray.data.range(4)
    output_ds = batch_predictor.predict(test_dataset)
    assert output_ds.to_pandas().to_numpy().squeeze().tolist() == [
        0.0,
        2.0,
        4.0,
        6.0,
    ]

    preprocessor2 = DummyPreprocessor(2)
    batch_predictor.set_preprocessor(preprocessor2)
    assert batch_predictor.get_preprocessor() == preprocessor2

    output_ds = batch_predictor.predict(test_dataset)
    assert output_ds.to_pandas().to_numpy().squeeze().tolist() == [
        0.0,
        4.0,
        8.0,
        12.0,
    ]

    # Remove preprocessor
    batch_predictor.set_preprocessor(None)
    assert batch_predictor.get_preprocessor() is None

    output_ds = batch_predictor.predict(test_dataset)
    assert output_ds.to_pandas().to_numpy().squeeze().tolist() == [
        0.0,
        2.0,
        4.0,
        6.0,
    ]


def test_batch_prediction_large_predictor_kwarg():
    class StubPredictor(Predictor):
        def __init__(self, **kwargs):
            super().__init__()

        @classmethod
        def from_checkpoint(cls, checkpoint, **kwargs):
            return cls(**kwargs)

        def _predict_numpy(self, data):
            return data

    checkpoint = Checkpoint.from_dict({"spam": "ham"})
    predictor_kwargs = {"array": np.arange(1e8)}  # This array is 800MB large
    predictor = BatchPredictor.from_checkpoint(
        checkpoint, StubPredictor, **predictor_kwargs
    )
    dataset = ray.data.range(1)
    predictor.predict(dataset)


def test_separate_gpu_stage_pipelined(shutdown_only):
    if ray.is_initialized():
        ray.shutdown()
    ray.init(num_gpus=1)
    batch_predictor = BatchPredictor.from_checkpoint(
        Checkpoint.from_dict({"factor": 2.0, PREPROCESSOR_KEY: DummyPreprocessor()}),
        DummyPredictor,
    )
    ds = batch_predictor.predict_pipelined(
        ray.data.range(5),
        blocks_per_window=1,
        num_gpus_per_worker=1,
        separate_gpu_stage=True,
        allow_gpu=True,
    )
    out = [x["id"] for x in ds.iter_rows()]
    stats = ds.stats()
    assert "Stage 1 ReadRange->DummyPreprocessor:" in stats, stats
    assert "Stage 2 MapBatches(ScoringWrapper):" in stats, stats
    assert max(out) == 16.0, out

    ds = batch_predictor.predict_pipelined(
        ray.data.range(5),
        blocks_per_window=1,
        num_gpus_per_worker=1,
        separate_gpu_stage=False,
        allow_gpu=True,
    )
    out = [x["id"] for x in ds.iter_rows()]
    stats = ds.stats()
    assert "Stage 1 ReadRange:" in stats, stats
    assert "Stage 2 MapBatches(ScoringWrapper):" in stats, stats
    assert max(out) == 16.0, out


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main(["-v", __file__]))
