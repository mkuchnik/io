# Copyright 2018 The TensorFlow Authors. All Rights Reserved.
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
# ==============================================================================
"""ElasticsearchIODatasets"""

from urllib.parse import urlparse
import tensorflow as tf
from tensorflow_io.core.python.ops import core_ops


class _ElasticsearchHandler:
    """Utility class to facilitate API queries and state management of
        session data.
    """

    def __init__(self, nodes, index, doc_type):
        self.nodes = nodes
        self.index = index
        self.doc_type = doc_type
        self.prepare_base_urls()
        self.prepare_connection_data()

    def prepare_base_urls(self):
        """Prepares the base url for establish connection with the elasticsearch master

        Returns:
            A list of base_url's, each of type tf.string for establishing the connection pool
        """

        if self.nodes is None:
            self.nodes = ["http://localhost:9200"]

        elif isinstance(self.nodes, str):
            self.nodes = [self.nodes]

        self.base_urls = []
        for node in self.nodes:
            if "//" not in node:
                raise ValueError(
                    "Please provide the list of nodes in 'protocol://host:port' format."
                )

            # Additional sanity check
            url_obj = urlparse(node)
            base_url = "{}://{}".format(url_obj.scheme, url_obj.netloc)
            self.base_urls.append(base_url)

        return self.base_urls

    def prepare_connection_data(self):
        """Prepares the healthcheck and resource urls from the base_urls"""

        self.healthcheck_urls = [
            "{}/_cluster/health".format(base_url) for base_url in self.base_urls
        ]
        self.request_urls = []
        for base_url in self.base_urls:
            if self.doc_type is None:
                request_url = "{}/{}/_search?scroll=1m".format(base_url, self.index)
            else:
                request_url = "{}/{}/{}/_search?scroll=1m".format(
                    base_url, self.index, self.doc_type
                )
            self.request_urls.append(request_url)

        return self.healthcheck_urls, self.request_urls

    def get_healthy_resource(self):
        """Retrieve the resource which is connected to a healthy node"""

        for healthcheck_url, request_url in zip(
            self.healthcheck_urls, self.request_urls
        ):
            try:
                resource, columns, raw_dtypes = core_ops.io_elasticsearch_readable_init(
                    healthcheck_url=healthcheck_url,
                    healthcheck_field="status",
                    request_url=request_url,
                )
                print("Connection successful: {}".format(healthcheck_url))
                dtypes = []
                for dtype in raw_dtypes:
                    if dtype == "DT_INT32":
                        dtypes.append(tf.int32)
                    elif dtype == "DT_INT64":
                        dtypes.append(tf.int64)
                    elif dtype == "DT_DOUBLE":
                        dtypes.append(tf.double)
                    elif dtype == "DT_STRING":
                        dtypes.append(tf.string)
                return resource, columns, dtypes, request_url
            except Exception:
                print("Skipping host: {}".format(healthcheck_url))
                continue
        else:
            raise ConnectionError(
                "No healthy node available for this index, check the cluster status and index"
            )

    def prepare_next_batch(self, resource, columns, dtypes, request_url):
        """Prepares the next batch of data based on the request url and
        the counter index.

        Args:
            resource: the init op resource.
            columns: list of columns to prepare the structured data.
            dtypes: tf.dtypes of the columns.
            request_url: The request url to fetch the data
        Returns:
            Structured data which columns as keys and the corresponding tensors as values.
        """

        url_obj = urlparse(request_url)
        scroll_request_url = "{}://{}/_search/scroll".format(
            url_obj.scheme, url_obj.netloc
        )

        values = core_ops.io_elasticsearch_readable_next(
            resource=resource,
            request_url=request_url,
            scroll_request_url=scroll_request_url,
            dtypes=dtypes,
        )
        data = {}
        for i, column in enumerate(columns.numpy()):
            data[column.decode("utf-8")] = values[i]
        return data


class ElasticsearchIODataset(tf.compat.v2.data.Dataset):
    """Represents an elasticsearch based tf.data.Dataset"""

    def __init__(self, nodes, index, doc_type=None, internal=True):
        """Prepare the ElasticsearchIODataset.

        Args:
            nodes: A `tf.string` tensor containing the hostnames of nodes
                in [protocol://hostname:port] format.
                For example: ["http://localhost:9200"]
            index: A `tf.string` representing the elasticsearch index to query.
            doc_type: A `tf.string` representing the type of documents in the index
                to query.
        """
        with tf.name_scope("ElasticsearchIODataset"):
            assert internal

            handler = _ElasticsearchHandler(nodes=nodes, index=index, doc_type=doc_type)
            resource, columns, dtypes, request_url = handler.get_healthy_resource()

            dataset = tf.data.experimental.Counter()
            dataset = dataset.map(
                lambda i: handler.prepare_next_batch(
                    resource=resource,
                    columns=columns,
                    dtypes=dtypes,
                    request_url=request_url,
                )
            )
            dataset = dataset.apply(
                tf.data.experimental.take_while(
                    lambda v: tf.greater(
                        tf.shape(v[columns.numpy()[0].decode("utf-8")])[0], 0
                    )
                )
            )
            self._dataset = dataset

            super().__init__(
                self._dataset._variant_tensor
            )  # pylint: disable=protected-access

    def _inputs(self):
        return []

    @property
    def element_spec(self):
        return self._dataset.element_spec
