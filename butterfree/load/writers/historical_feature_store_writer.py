"""Holds the Historical Feature Store writer class."""

import os

from pyspark.sql.dataframe import DataFrame
from pyspark.sql.functions import dayofmonth, month, year

from butterfree.clients import SparkClient
from butterfree.configs import environment
from butterfree.configs.db import S3Config
from butterfree.constants import columns
from butterfree.constants.spark_constants import DEFAULT_NUM_PARTITIONS
from butterfree.dataframe_service import repartition_df
from butterfree.hooks import Hook
from butterfree.hooks.schema_compatibility import SparkTableSchemaCompatibilityHook
from butterfree.load.writers.writer import Writer
from butterfree.transform import FeatureSet


class HistoricalFeatureStoreWriter(Writer):
    """Enable writing feature sets into the Historical Feature Store.

    Attributes:
        db_config: Datalake configuration for Spark, by default on AWS S3.
            For more information check module 'butterfree.db.configs'.
        database: database name to use in Spark metastore.
            By default FEATURE_STORE_HISTORICAL_DATABASE environment variable.
        num_partitions: value to use when applying repartition on the df before save.
        validation_threshold: lower and upper tolerance to using in count validation.
            The default value is defined in DEFAULT_VALIDATION_THRESHOLD property.
            For example: with a validation_threshold = 0.01 and a given calculated
            count on the dataframe equal to 100000 records, if the feature store
            return a count equal to 995000 an error will not be thrown.
            Use validation_threshold = 0 to not use tolerance in the validation.
        debug_mode: "dry run" mode, write the result to a temporary view.
        check_schema: hook to check the schemas between the existing table
            and the dataframe to be written.

    Example:
        Simple example regarding HistoricalFeatureStoreWriter class instantiation.
        We can instantiate this class without db configurations, so the class get the
        S3Config() where it provides default configurations about AWS S3 service.

    >>> spark_client = SparkClient()
    >>> writer = HistoricalFeatureStoreWriter()
    >>> writer.load(feature_set=feature_set,
       ...           dataframe=dataframe,
       ...           spark_client=spark_client)

        However, we can define the db configurations,
        like write mode, file format and S3 bucket,
        and provide them to HistoricalFeatureStoreWriter.

    >>> spark_client = SparkClient()
    >>> config = S3Config(bucket="my_s3_bucket_name",
        ...               mode="overwrite",
        ...               format_="parquet")
    >>> writer = HistoricalFeatureStoreWriter(db_config=config)
    >>> writer.load(feature_set=feature_set,
       ...           dataframe=dataframe,
       ...           spark_client=spark_client)

        For what settings you can use on S3Config and default settings,
        to read S3Config class.

        We can instantiate HistoricalFeatureStoreWriter class to validate the df
        to be written.

    >>> spark_client = SparkClient()
    >>> writer = HistoricalFeatureStoreWriter()
    >>> writer.validate(feature_set=feature_set,
       ...              dataframe=dataframe,
       ...              spark_client=spark_client)

        Both methods (write and validate) will need the Spark Client, Feature Set
        and DataFrame, to write or to validate, according to the Writer's arguments.

        P.S.(1): When writing, the HistoricalFeatureStoreWrite partitions the data to
        improve queries performance. The data is stored in partition folders in AWS S3
        based on time (per year, month and day).

        P.S.(2): HistoricalFeatureStoreWrite use Dynamic Partition Inserts,
        the behaviour of OVERWRITE keyword is controlled by
        spark.sql.sources.partitionOverwriteMode configuration property.
        The dynamic overwrite mode is enabled Spark will only delete the
        partitions for which it has data to be written to.
        All the other partitions remain intact.

    """

    PARTITION_BY = [
        columns.PARTITION_YEAR,
        columns.PARTITION_MONTH,
        columns.PARTITION_DAY,
    ]

    DEFAULT_VALIDATION_THRESHOLD = 0.01

    __name__ = "Historical Feature Store Writer"

    def __init__(
        self,
        db_config=None,
        database=None,
        num_partitions=None,
        validation_threshold: float = DEFAULT_VALIDATION_THRESHOLD,
        debug_mode: bool = False,
        check_schema: Hook = None,
    ):
        super().__init__(debug_mode)
        self.db_config = db_config or S3Config()
        self.check_schema = check_schema
        self.database = database or environment.get_variable(
            "FEATURE_STORE_HISTORICAL_DATABASE"
        )
        self.num_partitions = num_partitions or DEFAULT_NUM_PARTITIONS
        self.validation_threshold = validation_threshold

    def load(
        self, feature_set: FeatureSet, dataframe: DataFrame, spark_client: SparkClient,
    ):
        """Prepare the dataframe before it is saved to the Historical Feature Store.

        Args:
            feature_set: object processed with feature_set informations.
            dataframe: spark dataframe containing data from a feature set.
            spark_client: client for spark connections with external services.

        Returns:
            load_df: Dataframe ready to be saved.
            db_config: Spark configuration for connect databases.
            options(optional = None): All other string options.
            database(optional = None): Database name where the dataframe will be saved.
            table_name: Table name where the dataframe will be saved.
            partition_by(optional = None): Partition column to use when writing.
        """
        if not self.check_schema:
            self.check_schema = SparkTableSchemaCompatibilityHook(
                spark_client, feature_set.name, self.database
            )

        self.add_pre_hook(self.check_schema)

        dataframe = self._create_partitions(dataframe)

        if not self.debug_mode:
            partition_overwrite_mode = spark_client.conn.conf.get(
                "spark.sql.sources.partitionOverwriteMode"
            ).lower()

            if partition_overwrite_mode != "dynamic":
                raise RuntimeError(
                    "m=load_incremental, "
                    "spark.sql.sources.partitionOverwriteMode={}, "
                    "msg=partitionOverwriteMode "
                    "have to be configured to 'dynamic'".format(
                        partition_overwrite_mode
                    )
                )

        s3_key = os.path.join("historical", feature_set.entity, feature_set.name)
        options = {"path": self.db_config.get_options(s3_key).get("path")}

        return (
            dataframe,
            self.db_config,
            options,
            self.database,
            feature_set.name,
            self.PARTITION_BY,
        )

    def _assert_validation_count(self, table_name, written_count, dataframe_count):
        lower_bound = (1 - self.validation_threshold) * written_count
        upper_bound = (1 + self.validation_threshold) * written_count
        validation = lower_bound <= dataframe_count <= upper_bound
        assert validation, (
            "Data written to the Historical Feature Store and read back "
            f"from {table_name} has a different count than the feature set dataframe. "
            f"\nNumber of rows in {table_name}: {written_count}."
            f"\nNumber of rows in the dataframe: {dataframe_count}."
        )

    def validate(
        self, feature_set: FeatureSet, dataframe: DataFrame, spark_client: SparkClient
    ):
        """Calculate dataframe rows to validate data into Feature Store.

        Args:
            feature_set: object processed with feature_set informations.
            dataframe: spark dataframe containing data from a feature set.
            spark_client: client for spark connections with external services.

        Raises:
            AssertionError: if count of written data doesn't match count in current
                feature set dataframe.

        """
        table_name = (
            f"{self.database}.{feature_set.name}"
            if not self.debug_mode
            else f"historical_feature_store__{feature_set.name}"
        )

        written_count = (
            spark_client.read(
                self.db_config.format_, options=self.db_config.get_options(table_name)
            ).count()
            if not self.debug_mode
            else spark_client.read_table(table_name).count()
        )
        dataframe_count = dataframe.count()
        self._assert_validation_count(table_name, written_count, dataframe_count)

    def _create_partitions(self, dataframe):
        # create year partition column
        dataframe = dataframe.withColumn(
            columns.PARTITION_YEAR, year(dataframe[columns.TIMESTAMP_COLUMN])
        )
        # create month partition column
        dataframe = dataframe.withColumn(
            columns.PARTITION_MONTH, month(dataframe[columns.TIMESTAMP_COLUMN])
        )
        # create day partition column
        dataframe = dataframe.withColumn(
            columns.PARTITION_DAY, dayofmonth(dataframe[columns.TIMESTAMP_COLUMN])
        )
        return repartition_df(dataframe, self.PARTITION_BY, self.num_partitions)
