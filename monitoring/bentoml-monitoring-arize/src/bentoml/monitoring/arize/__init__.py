from __future__ import annotations

import os
import typing as t
import logging
import datetime
import functools
import collections
from enum import Enum
from enum import unique

import attr
from arize.api import Client
from arize.utils.types import Embedding
from arize.utils.types import ModelTypes
from arize.utils.types import Environments

from bentoml.monitoring import MonitorBase

BENTOML_MONITOR_ROLES = {"feature", "prediction", "target"}
BENTOML_MONITOR_TYPES = {"numerical", "categorical", "numerical_sequence"}
logger = logging.getLogger(__name__)
DataType = t.Union[str, int, float, bool, t.List[float]]


@unique
class Mapping(Enum):
    """
    Mapping solutions for bentoml data fields to arize data fields
    """

    SCORED_CLASSIFICATION = 1
    CLASSIFICATION = 2
    REGRESSION = 3
    RANKING = 4


@attr.define(auto_attribs=True)
class _FieldStats:
    prediction_label_columns: list[str] = attr.field(factory=list)
    prediction_score_columns: list[str] = attr.field(factory=list)
    actual_label_columns: list[str] = attr.field(factory=list)
    actual_score_columns: list[str] = attr.field(factory=list)
    feature_columns: list[str] = attr.field(factory=list)
    embedding_feature_columns: list[str] = attr.field(factory=list)


def _stat_fields(schema: list[dict[str, str]]) -> _FieldStats:
    fields = _FieldStats()
    for column in schema:
        if column["role"] == "feature":
            if column["type"] == "numerical_sequence":
                fields.embedding_feature_columns.append(column["name"])
            else:
                fields.feature_columns.append(column["name"])
        elif column["type"] == "categorical" and column["role"] == "prediction":
            fields.prediction_label_columns.append(column["name"])
        elif column["type"] == "numerical" and column["role"] == "prediction":
            fields.prediction_score_columns.append(column["name"])
        elif column["type"] == "numerical_sequence" and column["role"] == "prediction":
            logger.warning(
                "Arize Monitor does not support numerical_sequence for prediction. "
                "Ignoring column %s",
                column["name"],
            )
        elif column["type"] == "categorical" and column["role"] == "target":
            fields.actual_label_columns.append(column["name"])
        elif column["type"] == "numerical" and column["role"] == "target":
            fields.actual_score_columns.append(column["name"])
        elif column["type"] == "numerical_sequence" and column["role"] == "target":
            logger.warning(
                "Arize Monitor does not support numerical_sequence for target. "
                "Ignoring column %s",
                column["name"],
            )
        else:
            logger.warning(
                "Arize Monitor does not support column %s with role %s and type %s."
                "Ignoring column",
                column["name"],
                column["role"],
                column["type"],
            )
    return fields


def _is_valid_classification_form(fields: _FieldStats, warn: bool = False) -> bool:
    if fields.prediction_label_columns and not fields.prediction_score_columns:
        if warn and len(fields.prediction_label_columns) > 1:
            logger.warning(
                "Arize only supports single prediction label column, column %s will be ignore",
                fields.prediction_label_columns[1:],
            )
        return True
    if fields.actual_label_columns and not fields.actual_score_columns:
        if warn and len(fields.actual_label_columns) > 1:
            logger.warning(
                "Arize only supports single actual label column, column %s will be ignore",
                fields.actual_label_columns[1:],
            )
        return True
    return False


def _is_valid_scored_classification_form(
    fields: _FieldStats, warn: bool = False
) -> bool:
    if fields.prediction_label_columns and fields.prediction_score_columns:
        if warn and len(fields.prediction_label_columns) > 1:
            logger.warning(
                "Arize only supports single prediction label column, column %s will be ignore",
                fields.prediction_label_columns[1:],
            )
        if warn and len(fields.prediction_score_columns) > 1:
            logger.warning(
                "Arize only supports single prediction score column, column %s will be ignore",
                fields.prediction_score_columns[1:],
            )
        return True
    if fields.actual_label_columns and fields.actual_score_columns:
        if warn and len(fields.actual_label_columns) > 1:
            logger.warning(
                "Arize only supports single actual label column, column %s will be ignore",
                fields.actual_label_columns[1:],
            )
        if warn and len(fields.actual_score_columns) > 1:
            logger.warning(
                "Arize only supports single actual score column, column %s will be ignore",
                fields.actual_score_columns[1:],
            )
        return True
    return False


def _is_valid_regression_form(fields: _FieldStats, warn: bool = False) -> bool:
    assert fields.is_filled
    if fields.prediction_score_columns and not fields.prediction_label_columns:
        if warn and len(fields.prediction_score_columns) > 1:
            logger.warning(
                "Arize only supports single prediction score column, column %s will be ignore",
                fields.prediction_score_columns[1:],
            )
        return True
    if fields.actual_score_columns and not fields.actual_label_columns:
        if warn and len(fields.actual_score_columns) > 1:
            logger.warning(
                "Arize only supports single actual score column, column %s will be ignore",
                fields.actual_score_columns[1:],
            )
        return True
    return False


def _infer_mapping(
    fields: _FieldStats,
    model_type: ModelTypes | None = None,
) -> Mapping:
    """
    Infer the mapping solution for bentoml data fields to arize data fields
    https://docs.arize.com/arize/model-schema-mapping#performance-metrics
    """
    if model_type is None:
        if _is_valid_scored_classification_form(fields):
            mapping = Mapping.SCORED_CLASSIFICATION
        elif _is_valid_classification_form(fields):
            mapping = Mapping.CLASSIFICATION
        elif _is_valid_regression_form(fields):
            mapping = Mapping.REGRESSION
        else:
            raise ValueError(
                "failed to find a valid mapping to arize schema for the given schema. "
                "Please specify a mapping using the `model_type` parameter."
            )
    elif model_type == ModelTypes.SCORE_CATEGORICAL:
        if _is_valid_scored_classification_form(fields, warn=True):
            mapping = Mapping.SCORED_CLASSIFICATION
        elif _is_valid_classification_form(fields, warn=True):
            mapping = Mapping.CLASSIFICATION
        else:
            raise ValueError("Not a valid arize classification schema")
    elif model_type == ModelTypes.NUMERIC:
        if _is_valid_regression_form(fields, warn=True):
            mapping = Mapping.REGRESSION
        else:
            raise ValueError("Not a valid arize regression schema")
    else:
        logger.warning(
            "Arize Monitor does not support model type %s. Falling back to default mapping"
        )
        mapping = Mapping.REGRESSION
    return mapping


_mapping_to_model_type = {
    Mapping.SCORED_CLASSIFICATION: ModelTypes.SCORE_CATEGORICAL,
    Mapping.CLASSIFICATION: ModelTypes.SCORE_CATEGORICAL,
    Mapping.REGRESSION: ModelTypes.NUMERIC,
}


def _map_data(
    record: dict[str, DataType], fields: _FieldStats, mapping: Mapping
) -> tuple:
    """
    Map bentoml monitoring record to arize fields
    """
    if mapping == Mapping.SCORED_CLASSIFICATION:
        prediction_label = (
            record[fields.prediction_label_columns[0]],
            record[fields.prediction_score_columns[0]],
        )
        actual_label = (
            record[fields.actual_label_columns[0]],
            record[fields.actual_score_columns[0]],
        )
    elif mapping == Mapping.CLASSIFICATION:
        prediction_label = record[fields.prediction_label_columns[0]]
        actual_label = record[fields.actual_label_columns[0]]
    elif mapping == Mapping.REGRESSION:
        prediction_label = record[
            (fields.prediction_score_columns + fields.prediction_label_columns)[0]
        ]
        actual_label = record[
            (fields.actual_score_columns + fields.actual_label_columns)[0]
        ]
    else:
        logger.warning("Mapping not supported. Fallback to regression")
        prediction_label = record[fields.prediction_score_columns[0]]
        actual_label = record[fields.actual_score_columns[0]]
    features = {c: record[c] for c in fields.feature_columns}
    embedding_features = {
        c: Embedding(vector=record[c]) for c in fields.embedding_feature_columns  # type: ignore
    }
    return prediction_label, actual_label, features, embedding_features


class ArizeMonitor(MonitorBase[DataType]):
    """ """

    PRESERVED_COLUMNS = (COLUMN_TIME, COLUMN_RID) = ("timestamp", "request_id")

    def __init__(
        self,
        name: str,
        api_key: str | None = None,
        space_key: str | None = None,
        uri: str = "https://api.arize.com/v1",
        max_workers: int = 1,
        max_queue_bound: int = 5000,
        timeout: int = 200,
        model_type: ModelTypes | None = None,
        model_id: str | None = None,
        model_version: str | None = None,
        environment: Environments | None = None,
        model_tags: dict[str, str | bool | float | int] | None = None,
    ):
        self.name = name

        # client options
        if api_key is None:
            api_key = os.environ.get("ARIZE_API_KEY")
        assert api_key is not None, "api_key is required"
        self.api_key = api_key
        if space_key is None:
            space_key = os.environ.get("ARIZE_SPACE_KEY")
        assert space_key is not None, "space_key is required"
        self.space_key = space_key
        self.uri = uri
        self.max_workers = max_workers
        self.max_queue_bound = max_queue_bound
        self.timeout = timeout

        # model options
        self.model_type = model_type
        self.model_id = model_id
        self.model_version = model_version
        self.environment = environment
        self.model_tags = model_tags

        # internal state
        self._is_recording = False
        self._is_first_record = True
        self._is_first_column = False
        self._schema: list[dict[str, str]] = []
        self._arize_schema: list[dict[str, str]] = []
        self._columns: dict[
            str,
            collections.deque[DataType],
        ] = collections.defaultdict(collections.deque)

    def _init_client(self):
        self._client = Client(
            api_key=self.api_key,
            space_key=self.space_key,
            uri=self.uri,
            max_workers=self.max_workers,
            max_queue_bound=self.max_queue_bound,
            timeout=self.timeout,
        )

    def start_record(self) -> None:
        """
        Start recording data. This method should be called before logging any data.
        """
        self._is_first_column = True

    def stop_record(self) -> None:
        """
        Stop recording data. This method should be called after logging all data.
        """
        if self._is_first_record:
            self.export_schema()
            self._is_first_record = False

        if self._is_first_column:
            logger.warning("No data logged in this record. Will skip this record.")
        else:
            self.export_data()

    def export_schema(self):
        """
        Export schema of the data. This method should be called right after the first record.
        """
        fields = _stat_fields(self._schema)
        mapping = _infer_mapping(fields, self.model_type)
        self._data_converter = functools.partial(
            _map_data, fields=fields, mapping=mapping
        )

        if self.model_type is None:
            self.model_type = _mapping_to_model_type[mapping]

        if self.model_version is None and self.model_id is None:
            from bentoml._internal.context import component_context

            self.model_id = component_context.bento_name
            self.model_version = component_context.bento_version

        if self.environment is None:
            self.environment = Environments.PRODUCTION

        self._init_client()

    def export_data(self):
        """
        Export data. This method should be called after all data is logged.
        """
        assert (
            len(set(len(q) for q in self._columns.values())) == 1
        ), "All columns must have the same length"
        assert self.model_id is not None
        assert self.model_type is not None
        assert self.environment is not None
        while True:
            try:
                record = {k: v.popleft() for k, v in self._columns.items()}
                timestamp = record[self.COLUMN_TIME]
                assert isinstance(timestamp, float)
                prediction_id = record[self.COLUMN_RID]
                assert isinstance(prediction_id, int)
                data_infos = self._data_converter(record)
                self._client.log(
                    model_id=self.model_id,
                    model_type=self.model_type,
                    environment=self.environment,
                    model_version=self.model_version,
                    tags=self.model_tags,
                    prediction_id=prediction_id,
                    prediction_timestamp=int(timestamp),
                    batch_id=None,
                    prediction_label=data_infos[0],
                    actual_label=data_infos[1],
                    features=data_infos[2],
                    embedding_features=data_infos[3],
                )
            except IndexError:
                break

    def log(
        self,
        data: DataType,
        name: str,
        role: str,
        data_type: str,
    ) -> None:
        """
        log a data with column name, role and type to the current record
        """
        if name in self.PRESERVED_COLUMNS:
            raise ValueError(
                f"Column name {name} is preserved. Please use a different name."
            )

        assert role in BENTOML_MONITOR_ROLES, f"Invalid role {role}"
        assert data_type in BENTOML_MONITOR_TYPES, f"Invalid data type {data_type}"

        if self._is_first_record:
            self._schema.append({"name": name, "role": role, "type": data_type})
        if self._is_first_column:
            self._is_first_column = False

            from bentoml._internal.context import trace_context

            # universal columns
            self._columns[self.COLUMN_TIME].append(datetime.datetime.now().timestamp())
            assert trace_context.request_id is not None
            self._columns[self.COLUMN_RID].append(trace_context.request_id)

        self._columns[name].append(data)

    def log_batch(
        self,
        data_batch: t.Iterable[DataType],
        name: str,
        role: str,
        data_type: str,
    ) -> None:
        """
        Log a batch of data. The data will be logged as a single column.
        """
        try:
            for data in data_batch:
                self.log(data, name, role, data_type)
        except TypeError:
            raise ValueError(
                "data_batch is not iterable. Please use log() to log a single data."
            ) from None

    def log_table(
        self,
        data: t.Any,  # type: pandas.DataFrame
        schema: list[dict[str, str]],
    ) -> None:
        raise NotImplementedError("Not implemented yet")
