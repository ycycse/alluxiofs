# The Alluxio Open Foundation licenses this work under the Apache License, version 2.0
# (the "License"). You may not use this work except in compliance with the License, which is
# available at www.apache.org/licenses/LICENSE-2.0
#
# This software is distributed on an "AS IS" basis, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND,
# either express or implied, as more fully set forth in the License.
#
# See the NOTICE file distributed with this work for information regarding copyright ownership.
import asyncio
import hashlib
import io
import json
import logging
import re
import time
import weakref
from dataclasses import dataclass
from enum import Enum
from typing import Dict
from typing import Tuple

import aiohttp
import humanfriendly
import requests
from requests.adapters import HTTPAdapter

from .utils import set_log_level

try:
    from alluxiocommon import _DataManager
except ModuleNotFoundError:
    print(
        "[WARNING]pkg 'alluxiocommon' not installed, relative modules unable to invoke."
    )

from .config import AlluxioClientConfig
from .const import (
    ALLUXIO_HASH_NODE_PER_WORKER_DEFAULT_VALUE,
    MKDIR_URL_FORMAT,
    TOUCH_URL_FORMAT,
    TAIL_URL_FORMAT,
    HEAD_URL_FORMAT,
    MV_URL_FORMAT,
    RM_URL_FORMAT,
    CP_URL_FORMAT,
    FULL_CHUNK_URL_FORMAT,
    WRITE_CHUNK_URL_FORMAT,
    FULL_RANGE_URL_FORMAT,
)
from .const import ALLUXIO_COMMON_ONDEMANDPOOL_DISABLE
from .const import ALLUXIO_COMMON_EXTENSION_ENABLE
from .const import ALLUXIO_PAGE_SIZE_DEFAULT_VALUE
from .const import ALLUXIO_PAGE_SIZE_KEY
from .const import ALLUXIO_SUCCESS_IDENTIFIER
from .const import FULL_PAGE_URL_FORMAT
from .const import GET_FILE_STATUS_URL_FORMAT
from .const import LIST_URL_FORMAT
from .const import LOAD_PROGRESS_URL_FORMAT
from .const import LOAD_SUBMIT_URL_FORMAT
from .const import LOAD_URL_FORMAT
from .const import PAGE_URL_FORMAT
from .const import WRITE_PAGE_URL_FORMAT
from .worker_ring import ConsistentHashProvider

logger = logging.getLogger(__name__)


@dataclass
class AlluxioPathStatus:
    type: str
    name: str
    path: str
    ufs_path: str
    last_modification_time_ms: int
    human_readable_file_size: str
    length: int
    content_hash: str = None


class LoadState(Enum):
    RUNNING = "RUNNING"
    VERIFYING = "VERIFYING"
    STOPPED = "STOPPED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class Method(Enum):
    GET = "GET"
    POST = "POST"
    PUT = "PUT"
    DELETE = "DELETE"
    HEAD = "HEAD"
    OPTIONS = "OPTIONS"
    PATCH = "PATCH"


class OpType(Enum):
    SUBMIT = "submit"
    PROGRESS = "progress"
    STOP = "stop"


class AlluxioClient:
    """
    Access Alluxio file system

    Examples
    --------
    >>> # Launch Alluxio with ETCD as service discovery
    >>> alluxio = AlluxioClient(etcd_hosts="localhost")
    >>> # Or launch Alluxio with user provided worker list
    >>> alluxio = AlluxioClient(worker_hosts="host1,host2,host3")

    >>> print(alluxio.listdir("s3://mybucket/mypath/dir"))
    [
        {
            type: "file",
            name: "my_file_name",
            path: '/my_file_name',
            ufs_path: 's3://example-bucket/my_file_name',
            last_modification_time_ms: 0,
            length: 77542,
            human_readable_file_size: '75.72KB'
        },

    ]
    >>> print(alluxio.read("s3://mybucket/mypath/dir/myfile"))
    my_file_content
    """

    def __init__(
        self,
        **kwargs,
    ):
        """
        Inits Alluxio file system.

        Args:
            etcd_hosts (str, optional):
                The hostnames of ETCD to get worker addresses from
                The hostnames in host1,host2,host3 format. Either etcd_hosts or worker_hosts should be provided, not both.
            worker_hosts (str, optional):
                The worker hostnames in host1,host2,host3 format. Either etcd_hosts or worker_hosts should be provided, not both.
            options (dict, optional):
                A dictionary of Alluxio property key and values.
                Note that Alluxio Python API only support a limited set of Alluxio properties.
            concurrency (int, optional):
                The maximum number of concurrent operations for HTTP requests. Default to 64.
            etcd_port (int, optional):
                The port of each etcd server.
            worker_http_port (int, optional):
                The port of the HTTP server on each Alluxio worker node.
            etcd_refresh_workers_interval(int, optional):
                The interval to refresh worker list from ETCD membership service periodically. All negative values mean the service is disabled.

        """

        self.config = AlluxioClientConfig(**kwargs)
        self.session = self._create_session(self.config.concurrency)
        self.hash_provider = ConsistentHashProvider(self.config)
        self.data_manager = None
        if kwargs.get(ALLUXIO_COMMON_EXTENSION_ENABLE, False):
            logger.info("alluxiocommon extension enabled.")
            self.data_manager = _DataManager(
                self.config.concurrency,
                ondemand_pool_disabled=kwargs.get(
                    ALLUXIO_COMMON_ONDEMANDPOOL_DISABLE, False
                ),
            )

        test_options = kwargs.get("test_options", {})
        set_log_level(logger, test_options)

    def listdir(self, path):
        """
        Lists the directory.

        Args:
            path (str): The full ufs path to list from

        Returns:
            list of dict: A list containing dictionaries, where each dictionary has:
                - type (str): 'directory' or 'file'.
                - name (str): Name of the directory/file.
                - path (str): Path of the directory/file.
                - ufs_path (str): UFS path of the directory/file.
                - last_modification_time_ms (int): Last modification time in milliseconds.
                - length (int): Length of the file or 0 for directory.
                - human_readable_file_size (str): Human-readable file size.

        Example:
            [
                {
                    type: "file",
                    name: "my_file_name",
                    path: '/my_file_name',
                    ufs_path: 's3://example-bucket/my_file_name',
                    last_modification_time_ms: 0,
                    length: 77542,
                    human_readable_file_size: '75.72KB'
                },
                {
                    type: "directory",
                    name: "my_dir_name",
                    path: '/my_dir_name',
                    ufs_path: 's3://example-bucket/my_dir_name',
                    last_modification_time_ms: 0,
                    length: 0,
                    human_readable_file_size: '0B'
                },
            ]
        """
        self._validate_path(path)
        worker_host, worker_http_port = self._get_preferred_worker_address(
            path
        )
        params = {"path": path}
        try:
            response = self.session.get(
                LIST_URL_FORMAT.format(
                    worker_host=worker_host, http_port=worker_http_port
                ),
                params=params,
            )
            response.raise_for_status()
            result = []
            for data in json.loads(response.content):
                result.append(
                    AlluxioPathStatus(
                        data["mType"],
                        data["mName"],
                        data["mPath"],
                        data["mUfsPath"],
                        data["mLastModificationTimeMs"],
                        data["mHumanReadableFileSize"],
                        data["mLength"],
                    )
                )
            return result
        except Exception as e:
            raise Exception(
                f"Error when listing path {path}: error {e}"
            ) from e

    def get_file_status(self, path):
        """
        Gets the file status of the path.

        Args:
            path (str): The full ufs path to get the file status of

        Returns:
            File Status: The struct has:
                - type (string): directory or file
                - name (string): name of the directory/file
                - path (string): the path of the file
                - ufs_path (string): the ufs path of the file
                - last_modification_time_ms (long): the last modification time
                - length (integer): length of the file or 0 for directory
                - human_readable_file_size (string): the size of the human readable files
                - content_hash (string): the hash of the file content

        Example:
            {
                type: 'directory',
                name: 'a',
                path: '/a',
                ufs_path: 's3://example-bucket/a',
                last_modification_time_ms: 0,
                length: 0,
                human_readable_file_size: '0B'
                content_hash: 'd41d8cd98f00b204e9800998ecf8427e'
            }
        """
        self._validate_path(path)
        worker_host, worker_http_port = self._get_preferred_worker_address(
            path
        )
        params = {"path": path}
        try:
            response = self.session.get(
                GET_FILE_STATUS_URL_FORMAT.format(
                    worker_host=worker_host,
                    http_port=worker_http_port,
                ),
                params=params,
            )
            response.raise_for_status()
            data = json.loads(response.content)[0]
            return AlluxioPathStatus(
                data["mType"],
                data["mName"],
                data["mPath"],
                data["mUfsPath"],
                data["mLastModificationTimeMs"],
                data["mHumanReadableFileSize"],
                data["mLength"],
                data["mContentHash"].strip('"'),
            )
        except Exception as e:
            raise Exception(
                f"Error when getting file status path {path}: error {e}"
            ) from e

    def load(
        self,
        path,
        timeout=None,
        verbose=True,
    ):
        """
        Loads a file.

        Args:
            path (str): The full path with storage protocol to load data from
            timeout (integer): The number of seconds for timeout, optional
            verbose (boolean): Whether enabling verbose load logging, default `True`

        Returns:
            result (boolean): Whether the file has been loaded successfully
        """
        self._validate_path(path)
        worker_host, worker_http_port = self._get_preferred_worker_address(
            path
        )
        return self._load_file(
            worker_host, worker_http_port, path, timeout, verbose
        )

    def submit_load(
        self,
        path,
        verbose=True,
    ):
        """
        Submits a load job for a file.

        Args:
            path (str): The full ufs file path to load data from
            verbose (boolean): Whether enabling verbose load logging, default `True`

        Returns:
            result (boolean): Whether the job has been submitted successfully
        """
        self._validate_path(path)
        worker_host, worker_http_port = self._get_preferred_worker_address(
            path
        )
        try:
            params = {
                "path": path,
                "opType": OpType.SUBMIT.value,
                "verbose": json.dumps(verbose),
            }
            response = self.session.get(
                LOAD_URL_FORMAT.format(
                    worker_host=worker_host,
                    http_port=worker_http_port,
                ),
                params=params,
            )
            response.raise_for_status()
            content = json.loads(response.content.decode("utf-8"))
            return content[ALLUXIO_SUCCESS_IDENTIFIER]
        except Exception as e:
            raise Exception(
                f"Error when submitting load job for path {path} from {worker_host}: error {e}"
            ) from e

    def stop_load(
        self,
        path,
    ):
        """
        Stops a load job for a file.

        Args:
            path (str): The full ufs file path to load data from

        Returns:
            result (boolean): Whether the job has been stopped successfully
        """
        self._validate_path(path)
        worker_host, worker_http_port = self._get_preferred_worker_address(
            path
        )
        try:
            params = {"path": path, "opType": OpType.STOP.value}
            response = self.session.get(
                LOAD_URL_FORMAT.format(
                    worker_host=worker_host,
                    http_port=worker_http_port,
                ),
                params=params,
            )
            response.raise_for_status()
            content = json.loads(response.content.decode("utf-8"))
            return content[ALLUXIO_SUCCESS_IDENTIFIER]
        except Exception as e:
            raise Exception(
                f"Error when stopping load job for path {path} from {worker_host}: error {e}"
            ) from e

    def load_progress(
        self,
        path,
        verbose=True,
    ):
        """
        Gets the progress of the load job for a path.

        Args:
            path (str): The full UFS file path to load data from UFS to Alluxio.
            verbose (boolean): Whether enabling verbose load logging, default `True`

        Returns:
            LoadState: The current state of the load job as a LoadState enum. Possible values are:
                - LoadState.RUNNING: The load job is in progress.
                - LoadState.VERIFYING: The load job is verifying the loaded data.
                - LoadState.STOPPED: The load job has been stopped.
                - LoadState.SUCCEEDED: The load job completed successfully.
                - LoadState.FAILED: The load job failed.

        Example:
            load_state = alluxio_client.load_progress("s3://mybucket/mypath/file")
            print(f"Current Load State: {load_state.name}")
        """
        self._validate_path(path)
        worker_host, worker_http_port = self._get_preferred_worker_address(
            path
        )
        params = {
            "path": path,
            "opType": OpType.PROGRESS.value,
            "verbose": json.dumps(verbose),
        }
        load_progress_url = LOAD_URL_FORMAT.format(
            worker_host=worker_host,
            http_port=worker_http_port,
        )
        return self._load_progress_internal(load_progress_url, params)

    def read(self, file_path):
        """
        Reads the full file.

        Args:
            file_path (str): The full ufs file path to read data from

        Returns:
            file content (str): The full file content
        """
        try:
            file_status = self.get_file_status(file_path)
            if file_status is None:
                raise FileNotFoundError(f"File {file_path} not found")
            return self.read_range(file_path, 0, file_status.length)
        # self._validate_path(file_path)
        # worker_host, worker_http_port = self._get_preferred_worker_address(
        #     file_path
        # )
        # path_id = self._get_path_hash(file_path)
        # try:
        #     if self.data_manager:
        #         return b"".join(
        #             self._all_page_generator_alluxiocommon(
        #                 worker_host, worker_http_port, path_id, file_path
        #             )
        #         )
        #     else:
        #         return b"".join(
        #             self._all_page_generator(
        #                 worker_host, worker_http_port, path_id, file_path
        #             )
        #         )
        except Exception as e:
            raise Exception(
                f"Error when reading file {file_path}: error {e}"
            ) from e

    def read_file_range(self, file_path, offset=0, length=-1):
        """
        Reads the full file.

        Args:
            file_path (str): The full ufs file path to read data from
            offset (integer): The offset to start reading data from
            length (integer): The file length to read

        Returns:
            file content (str): The full file content
        """
        self._validate_path(file_path)
        worker_host, worker_http_port = self._get_preferred_worker_address(
            file_path
        )
        path_id = self._get_path_hash(file_path)
        try:
            if self.data_manager:
                self._all_chunk_generator_alluxiocommon(
                    worker_host, worker_http_port, path_id, file_path
                )
            else:
                return self._all_file_range_generator(
                    worker_host,
                    worker_http_port,
                    path_id,
                    file_path,
                    offset,
                    length,
                )
        except Exception as e:
            raise Exception(
                f"Error when reading file {file_path}: error {e}"
            ) from e

    def read_chunked(self, file_path, chunk_size=1024 * 1024):
        """
        Reads the full file.

        Args:
            file_path (str): The full ufs file path to read data from
            chunk_size (int, optional): The size of each chunk in bytes. Defaults to 1MB.

        Returns:
            file content (str): The full file content
        """
        self._validate_path(file_path)
        worker_host, worker_http_port = self._get_preferred_worker_address(
            file_path
        )
        path_id = self._get_path_hash(file_path)
        try:
            if self.data_manager:
                self._all_chunk_generator_alluxiocommon(
                    worker_host, worker_http_port, path_id, file_path
                )
            else:
                return self._all_chunk_generator(
                    worker_host,
                    worker_http_port,
                    path_id,
                    file_path,
                    chunk_size,
                )
        except Exception as e:
            raise Exception(
                f"Error when reading file {file_path}: error {e}"
            ) from e

    def _all_chunk_generator(
        self, worker_host, worker_http_port, path_id, file_path, chunk_size
    ):
        """
        Reads the full file.

        Args:
            worker_host (str): The worker host to read data from
            worker_http_port (int): The worker HTTP port to read data from
            path_id (int): The path id of the file
            file_path (str): The full ufs file path to read data from

        Returns:
            file content (str): The full file content
        """
        url_chunk = FULL_CHUNK_URL_FORMAT.format(
            worker_host=worker_host,
            http_port=worker_http_port,
            path_id=path_id,
            chunk_size=chunk_size,
            file_path=file_path,
            page_index=0,
        )
        out = io.BytesIO()
        headers = {"transfer-type": "chunked"}
        with requests.get(url_chunk, headers=headers, stream=True) as response:
            response.raise_for_status()
            for chunk in response.iter_content(chunk_size=chunk_size):
                if chunk:
                    out.write(chunk)
        out.seek(0)
        return out

    def read_range(self, file_path, offset, length):
        """
        Reads parts of a file.

        Args:
            file_path (str): The full ufs file path to read data from
            offset (integer): The offset to start reading data from
            length (integer): The file length to read

        Returns:
            file content (str): The file content with length from offset
        """
        logger.debug(f"read_range,off:{offset}:length:{length}")
        self._validate_path(file_path)
        if not isinstance(offset, int) or offset < 0:
            raise ValueError("Offset must be a non-negative integer")

        if length is None or length == -1:
            file_status = self.get_file_status(file_path)
            if file_status is None:
                raise FileNotFoundError(f"File {file_path} not found")
            length = file_status.length - offset

        if length == 0:
            return b""

        if not isinstance(length, int) or length < 0:
            raise ValueError(
                f"Invalid length: {length}. Length must be a non-negative integer, -1, or None. Requested offset: {offset}"
            )

        worker_host, worker_http_port = self._get_preferred_worker_address(
            file_path
        )
        path_id = self._get_path_hash(file_path)

        try:
            if self.data_manager:
                return self._range_page_generator_alluxiocommon(
                    worker_host,
                    worker_http_port,
                    path_id,
                    file_path,
                    offset,
                    length,
                )
            else:
                return b"".join(
                    self._range_page_generator(
                        worker_host,
                        worker_http_port,
                        path_id,
                        file_path,
                        offset,
                        length,
                    )
                )
        except Exception as e:
            raise Exception(
                f"Error when reading file:{file_path}: error:{e}: "
                f"worker_host:{worker_host}, worker_http_port:{worker_http_port}"
            ) from e

    def write(self, file_path, file_bytes):
        """
        Write a byte[] content to the file.
        Args:
            file_path (str): The full ufs file path to read data from
            file_bytes (bytes): The full ufs file content
        Returns:
            True if the write was successful, False otherwise.
        """
        self._validate_path(file_path)
        worker_host, worker_http_port = self._get_preferred_worker_address(
            file_path
        )
        path_id = self._get_path_hash(file_path)
        try:
            if self.data_manager:
                return b"".join(
                    self._all_page_generator_alluxiocommon(
                        worker_host, worker_http_port, path_id, file_path
                    )
                )
            else:
                return self._all_page_generator_write(
                    worker_host,
                    worker_http_port,
                    path_id,
                    file_path,
                    file_bytes,
                )
        except Exception as e:
            raise Exception(
                f"Error when reading file {file_path}: error {e}"
            ) from e

    def write_chunked(self, file_path, file_bytes, chunk_size=1024 * 1024):
        """
        Write a byte[] content to the file by chunked-transfer.
        Args:
            file_path (str): The full ufs file path to read data from
            file_bytes (bytes): The full ufs file content
            chunk_size (int, optional): The size of each chunk in bytes. Defaults to 1MB.
        Returns:
            True if the write was successful, False otherwise.
        """
        self._validate_path(file_path)
        worker_host, worker_http_port = self._get_preferred_worker_address(
            file_path
        )
        path_id = self._get_path_hash(file_path)
        try:
            if self.data_manager:
                return self._all_page_generator_alluxiocommon(
                    worker_host, worker_http_port, path_id, file_path
                )
            else:
                return self._all_chunk_generator_write(
                    worker_host,
                    worker_http_port,
                    path_id,
                    file_path,
                    file_bytes,
                    chunk_size,
                )
        except Exception as e:
            raise Exception(
                f"Error when reading file {file_path}: error {e}"
            ) from e

    def write_page(self, file_path, page_index, page_bytes):
        """
        Writes a page.

        Args:
            file_path: The path of the file where data is to be written.
            page_index: The page index in the file to write the data.
            page_bytes: The byte data to write to the specified page, MUST BE FULL PAGE.

        Returns:
            True if the write was successful, False otherwise.
        """
        self._validate_path(file_path)
        worker_host, worker_http_port = self._get_preferred_worker_address(
            file_path
        )
        path_id = self._get_path_hash(file_path)
        try:
            response = requests.post(
                WRITE_PAGE_URL_FORMAT.format(
                    worker_host=worker_host,
                    http_port=worker_http_port,
                    path_id=path_id,
                    file_path=file_path,
                    page_index=page_index,
                ),
                headers={"Content-Type": "application/octet-stream"},
                data=page_bytes,
            )
            response.raise_for_status()
            return 200 <= response.status_code < 300
        except requests.RequestException as e:
            raise Exception(
                f"Error writing to file {file_path} at page {page_index}: {e}"
            )

    def mkdir(self, file_path):
        """
        make a directory which path is 'file_path'.
        Args:
            file_path: The path of the directory to make.
        Returns:
            True if the mkdir was successful, False otherwise.
        """
        self._validate_path(file_path)
        worker_host, worker_http_port = self._get_preferred_worker_address(
            file_path
        )
        path_id = self._get_path_hash(file_path)
        try:
            response = requests.post(
                MKDIR_URL_FORMAT.format(
                    worker_host=worker_host,
                    http_port=worker_http_port,
                    path_id=path_id,
                    file_path=file_path,
                )
            )
            response.raise_for_status()
            return 200 <= response.status_code < 300
        except requests.RequestException as e:
            raise Exception(f"Error making a directory of {file_path}: {e}")

    def touch(self, file_path):
        """
        create a file which path is 'file_path'.
        Args:
            file_path: The path of the file to touch.
        Returns:
            True if the touch was successful, False otherwise.
        """
        self._validate_path(file_path)
        worker_host, worker_http_port = self._get_preferred_worker_address(
            file_path
        )
        path_id = self._get_path_hash(file_path)
        try:
            response = requests.post(
                TOUCH_URL_FORMAT.format(
                    worker_host=worker_host,
                    http_port=worker_http_port,
                    path_id=path_id,
                    file_path=file_path,
                )
            )
            response.raise_for_status()
            return 200 <= response.status_code < 300
        except requests.RequestException as e:
            raise Exception(f"Error create a file of {file_path}: {e}")

        # TODO(littelEast7): complete it

    def mv(self, path1, path2):
        """
        mv a file from path1 to path2.
        Args:
            path1: The path of the file original.
            path2: The path of the file destination.
        Returns:
            True if the mv was successful, False otherwise.
        """
        self._validate_path(path1)
        self._validate_path(path2)
        worker_host, worker_http_port = self._get_preferred_worker_address(
            path1
        )
        path_id = self._get_path_hash(path1)
        try:
            response = requests.post(
                MV_URL_FORMAT.format(
                    worker_host=worker_host,
                    http_port=worker_http_port,
                    path_id=path_id,
                    srcPath=path1,
                    dstPath=path2,
                )
            )
            response.raise_for_status()
            return 200 <= response.status_code < 300
        except requests.RequestException as e:
            raise Exception(f"Error move a file from {path1} to {path2}: {e}")

    def rm(self, path, option):
        """
        remove a file which path is 'path'.
        Args:
            path: The path of the file.
            option: The option to remove.
        Returns:
            True if the rm was successful, False otherwise.
        """
        self._validate_path(path)
        worker_host, worker_http_port = self._get_preferred_worker_address(
            path
        )
        path_id = self._get_path_hash(path)
        parameters = option.__dict__
        try:
            response = requests.post(
                RM_URL_FORMAT.format(
                    worker_host=worker_host,
                    http_port=worker_http_port,
                    path_id=path_id,
                    file_path=path,
                ),
                params=parameters,
            )
            response.raise_for_status()
            return 200 <= response.status_code < 300
        except requests.RequestException as e:
            raise Exception(f"Error remove a file {path}: {e}")

    def cp(self, path1, path2, option):
        """
        copy a file which path is 'path1' to 'path2'.
        Args:
            path1: The path of the file original.
            path2: The path of the file destination.
            option: The option to remove.
        Returns:
            True if the cp was successful, False otherwise.
        """
        self._validate_path(path1)
        worker_host, worker_http_port = self._get_preferred_worker_address(
            path1
        )
        path_id = self._get_path_hash(path1)
        parameters = option.__dict__
        try:
            response = requests.post(
                CP_URL_FORMAT.format(
                    worker_host=worker_host,
                    http_port=worker_http_port,
                    path_id=path_id,
                    srcPath=path1,
                    dstPath=path2,
                ),
                params=parameters,
            )
            response.raise_for_status()
            return 200 <= response.status_code < 300
        except requests.RequestException as e:
            raise Exception(f"Error copy a file from {path1} to {path2}: {e}")

    def tail(self, file_path, num_of_bytes=None):
        """
        show the tail a file which path is 'file_path'.
        Args:
            file_path: The ufs path of the file.
            num_of_bytes: The length of the file to show (like 1kb).
        Returns:
            The content of tail of the file.
        """
        self._validate_path(file_path)
        worker_host, worker_http_port = self._get_preferred_worker_address(
            file_path
        )
        path_id = self._get_path_hash(file_path)
        try:
            response = requests.get(
                TAIL_URL_FORMAT.format(
                    worker_host=worker_host,
                    http_port=worker_http_port,
                    path_id=path_id,
                    file_path=file_path,
                ),
                params={"numOfBytes": num_of_bytes},
            )
            return b"".join(response.iter_content())
        except requests.RequestException as e:
            raise Exception(f"Error show the tail of {file_path}: {e}")

    def head(self, file_path, num_of_bytes=None):
        """
        show the head a file which path is 'file_path'.
        Args:
            file_path: The ufs path of the file.
            num_of_bytes: The length of the file to show (like 1kb).
        Returns:
            The content of head of the file.
        """
        self._validate_path(file_path)
        worker_host, worker_http_port = self._get_preferred_worker_address(
            file_path
        )
        path_id = self._get_path_hash(file_path)
        try:
            response = requests.get(
                HEAD_URL_FORMAT.format(
                    worker_host=worker_host,
                    http_port=worker_http_port,
                    path_id=path_id,
                    file_path=file_path,
                ),
                params={"numBytes": num_of_bytes},
            )
            return b"".join(response.iter_content())
        except requests.RequestException as e:
            raise Exception(f"Error show the head of {file_path}: {e}")

    def _all_page_generator_alluxiocommon(
        self, worker_host, worker_http_port, path_id, file_path
    ):
        page_index = 0
        fetching_pages_num_each_round = 4
        while True:
            read_urls = []
            try:
                for _ in range(fetching_pages_num_each_round):
                    page_url = FULL_PAGE_URL_FORMAT.format(
                        worker_host=worker_host,
                        http_port=worker_http_port,
                        path_id=path_id,
                        file_path=file_path,
                        page_index=page_index,
                    )
                    read_urls.append(page_url)
                    page_index += 1
                pages_content = self.data_manager.make_multi_http_req(
                    read_urls
                )
                yield pages_content
                if (
                    len(pages_content)
                    < fetching_pages_num_each_round * self.config.page_size
                ):
                    break
            except Exception as e:
                # data_manager won't throw exception if there are any first few content retrieved
                # hence we always propagte exception from data_manager upwards
                raise Exception(
                    f"Error when reading all pages of {path_id}: error {e}"
                ) from e

    def _all_page_generator(
        self, worker_host, worker_http_port, path_id, file_path
    ):
        page_index = 0
        while True:
            try:
                page_content = self._read_page(
                    worker_host,
                    worker_http_port,
                    path_id,
                    file_path,
                    page_index,
                )
            except Exception as e:
                if page_index == 0:
                    raise Exception(
                        f"Error when reading page 0 of {path_id}: error {e}"
                    ) from e
                else:
                    # TODO(lu) distinguish end of file exception and real exception
                    break
            if not page_content:
                break
            yield page_content
            if len(page_content) < self.config.page_size:  # last page
                break
            page_index += 1

    def _all_page_generator_write(
        self, worker_host, worker_http_port, path_id, file_path, file_bytes
    ):
        page_index = 0
        page_size = self.config.page_size
        offset = 0
        try:
            while True:
                end = min(offset + page_size, len(file_bytes))
                page_bytes = file_bytes[offset:end]
                self._write_page(
                    worker_host,
                    worker_http_port,
                    path_id,
                    file_path,
                    page_index,
                    page_bytes,
                )
                page_index += 1
                offset += page_size
                if end >= len(file_bytes):
                    break
            return True
        except Exception as e:
            # data_manager won't throw exception if there are any first few content retrieved
            # hence we always propagte exception from data_manager upwards
            raise Exception(
                f"Error when writing all pages of {path_id}: error {e}"
            ) from e

    def _file_chunk_generator(self, file_bytes, chunk_size):
        offset = 0
        while offset < len(file_bytes):
            chunk = file_bytes[offset: offset + chunk_size]
            offset += chunk_size
            yield chunk

    def _all_chunk_generator_write(
        self,
        worker_host,
        worker_http_port,
        path_id,
        file_path,
        file_bytes,
        chunk_size,
    ):
        try:
            url = (
                WRITE_CHUNK_URL_FORMAT.format(
                    worker_host=worker_host,
                    http_port=worker_http_port,
                    path_id=path_id,
                    file_path=file_path,
                    chunk_size=chunk_size,
                ),
            )

            headers = {
                "transfer-type": "chunked",
                "Content-Type": "application/octet-stream",
            }
            response = requests.post(
                url[0],
                headers=headers,
                data=self._file_chunk_generator(file_bytes, chunk_size),
            )
            return response.status_code == 200
        except Exception as e:
            # data_manager won't throw exception if there are any first few content retrieved
            # hence we always propagte exception from data_manager upwards
            raise Exception(
                f"Error when writing all pages of {file_path}: error {e}"
            ) from e

    def _range_page_generator_alluxiocommon(
        self, worker_host, worker_http_port, path_id, file_path, offset, length
    ):
        read_urls = []
        start = offset
        while start < offset + length:
            page_index = start // self.config.page_size
            inpage_off = start % self.config.page_size
            inpage_read_len = min(
                self.config.page_size - inpage_off, offset + length - start
            )
            page_url = None
            if inpage_off == 0 and inpage_read_len == self.config.page_size:
                page_url = FULL_PAGE_URL_FORMAT.format(
                    worker_host=worker_host,
                    http_port=worker_http_port,
                    path_id=path_id,
                    file_path=file_path,
                    page_index=page_index,
                )
            else:
                page_url = PAGE_URL_FORMAT.format(
                    worker_host=worker_host,
                    http_port=worker_http_port,
                    path_id=path_id,
                    file_path=file_path,
                    page_index=page_index,
                    page_offset=inpage_off,
                    page_length=inpage_read_len,
                )
            read_urls.append(page_url)
            start += inpage_read_len
        data = self.data_manager.make_multi_http_req(read_urls)
        return data

    def _range_page_generator(
        self, worker_host, worker_http_port, path_id, file_path, offset, length
    ):
        start_page_index = offset // self.config.page_size
        start_page_offset = offset % self.config.page_size

        end_page_index = (offset + length - 1) // self.config.page_size
        end_page_read_to = ((offset + length - 1) % self.config.page_size) + 1

        page_index = start_page_index
        while True:
            try:
                read_offset = 0
                read_length = self.config.page_size
                if page_index == start_page_index:
                    read_offset = start_page_offset
                    if start_page_index == end_page_index:
                        read_length = end_page_read_to - start_page_offset
                    else:
                        read_length = self.config.page_size - start_page_offset
                elif page_index == end_page_index:
                    read_length = end_page_read_to

                page_content = self._read_page(
                    worker_host,
                    worker_http_port,
                    path_id,
                    file_path,
                    page_index,
                    read_offset,
                    read_length,
                )
                yield page_content

                # Check if it's the last page or the end of the file
                if (
                    page_index == end_page_index
                    or len(page_content) < read_length
                ):
                    break

                page_index += 1

            except Exception as e:
                if page_index == start_page_index:
                    raise Exception(
                        f"Error when reading page {page_index} of {path_id}: error {e}"
                    ) from e
                else:
                    # read some data successfully, return those data
                    break

    def _all_file_range_generator(
        self, worker_host, worker_http_port, path_id, file_path, offset, length
    ):
        try:
            url = FULL_RANGE_URL_FORMAT.format(
                worker_host=worker_host,
                http_port=worker_http_port,
                path_id=path_id,
                file_path=file_path,
                offset=offset,
                length=length,
            )
            response = requests.get(url)
            response.raise_for_status()
            return response.content
        except Exception as e:
            raise Exception(
                f"Error when reading file {path_id} with offset {offset} and length {length}: error {e}"
            ) from e

    def _create_session(self, concurrency):
        session = requests.Session()
        adapter = HTTPAdapter(
            pool_connections=concurrency, pool_maxsize=concurrency
        )
        session.mount("http://", adapter)
        return session

    def _load_file(
        self, worker_host, worker_http_port, path, timeout, verbose
    ):
        try:
            params = {
                "path": path,
                "opType": OpType.SUBMIT.value,
                "verbose": json.dumps(verbose),
            }
            response = self.session.get(
                LOAD_URL_FORMAT.format(
                    worker_host=worker_host,
                    http_port=worker_http_port,
                ),
                params=params,
            )
            response.raise_for_status()
            content = json.loads(response.content.decode("utf-8"))
            if not content[ALLUXIO_SUCCESS_IDENTIFIER]:
                return False

            params = {
                "path": path,
                "opType": OpType.PROGRESS.value,
                "verbose": json.dumps(verbose),
            }
            load_progress_url = LOAD_URL_FORMAT.format(
                worker_host=worker_host,
                http_port=worker_http_port,
            )
            stop_time = 0
            if timeout is not None:
                stop_time = time.time() + timeout
            while True:
                job_state, content = self._load_progress_internal(
                    load_progress_url, params
                )
                if job_state == LoadState.SUCCEEDED:
                    return True
                if job_state == LoadState.FAILED:
                    logger.error(
                        f"Failed to load path {path} with return message {content}"
                    )
                    return False
                if job_state == LoadState.STOPPED:
                    logger.warning(
                        f"Failed to load path {path} with return message {content}, load stopped"
                    )
                    return False
                if timeout is None or stop_time - time.time() >= 10:
                    time.sleep(10)
                else:
                    logger.debug(f"Failed to load path {path} within timeout")
                    return False

        except Exception as e:
            logger.debug(
                f"Error when loading file {path} from {worker_host} with timeout {timeout}: error {e}"
            )
            return False

    def _load_progress_internal(
        self, load_url: str, params: Dict
    ) -> (LoadState, str):
        try:
            response = self.session.get(load_url, params=params)
            response.raise_for_status()
            content = json.loads(response.content.decode("utf-8"))
            if "jobState" not in content:
                raise KeyError(
                    "The field 'jobState' is missing from the load progress response content"
                )
            state = content["jobState"]
            if "FAILED" in state:
                return LoadState.FAILED, content
            return LoadState(state), content
        except Exception as e:
            raise Exception(
                f"Error when getting load job progress for {load_url}: error {e}"
            ) from e

    def _read_page(
        self,
        worker_host,
        worker_http_port,
        path_id,
        file_path,
        page_index,
        offset=None,
        length=None,
    ):
        if (offset is None) != (length is None):
            raise ValueError(
                "Both offset and length should be either None or both not None"
            )

        try:
            if offset is None:
                page_url = FULL_PAGE_URL_FORMAT.format(
                    worker_host=worker_host,
                    http_port=worker_http_port,
                    path_id=path_id,
                    file_path=file_path,
                    page_index=page_index,
                )
                logger.debug(f"Reading full page request {page_url}")
            else:
                page_url = PAGE_URL_FORMAT.format(
                    worker_host=worker_host,
                    http_port=worker_http_port,
                    path_id=path_id,
                    file_path=file_path,
                    page_index=page_index,
                    page_offset=offset,
                    page_length=length,
                )
                logger.debug(f"Reading page request {page_url}")
            response = self.session.get(page_url)
            response.raise_for_status()
            return response.content

        except Exception as e:
            raise Exception(
                f"Error when requesting file {path_id} page {page_index} from {worker_host}: error {e}"
            ) from e

    def _write_page(
        self,
        worker_host,
        worker_http_port,
        path_id,
        file_path,
        page_index,
        page_bytes,
    ):
        """
        Writes a page.
        Args:
            file_path: The path of the file where data is to be written.
            page_index: The page index in the file to write the data.
            page_bytes: The byte data to write to the specified page, MUST BE FULL PAGE.
        Returns:
            True if the write was successful, False otherwise.
        """
        try:
            response = requests.post(
                WRITE_PAGE_URL_FORMAT.format(
                    worker_host=worker_host,
                    http_port=worker_http_port,
                    path_id=path_id,
                    file_path=file_path,
                    page_index=page_index,
                ),
                headers={"Content-Type": "application/octet-stream"},
                data=page_bytes,
            )
            response.raise_for_status()
            return 200 <= response.status_code < 300
        except requests.RequestException as e:
            raise Exception(
                f"Error writing to file {file_path} at page {page_index}: {e}"
            )

    def _get_path_hash(self, uri):
        hash_functions = [
            hashlib.sha256,
            hashlib.md5,
            lambda x: hex(hash(x))[2:].lower(),  # Fallback to simple hashCode
        ]
        for hash_function in hash_functions:
            try:
                hash_obj = hash_function()
                hash_obj.update(uri.encode("utf-8"))
                return hash_obj.hexdigest().lower()
            except AttributeError:
                continue

    def _get_preferred_worker_address(self, full_ufs_path):
        workers = self.hash_provider.get_multiple_workers(full_ufs_path, 1)
        if len(workers) != 1:
            raise ValueError(
                "Expected exactly one worker from hash ring, but found {} workers {}.".format(
                    len(workers), workers
                )
            )
        return workers[0].host, workers[0].http_server_port

    def _validate_path(self, path):
        if not isinstance(path, str):
            raise TypeError("path must be a string")

        if not re.search(r"^[a-zA-Z0-9]+://", path):
            raise ValueError(
                "path must be a full path with a protocol (e.g., 'protocol://path')"
            )


class AlluxioAsyncFileSystem:
    """
    Access Alluxio file system

    Examples
    --------
    >>> # Launch Alluxio with ETCD as service discovery
    >>> alluxio = AlluxioAsyncFileSystem(etcd_hosts="localhost")
    >>> # Or launch Alluxio with user provided worker list
    >>> alluxio = AlluxioAsyncFileSystem(worker_hosts="host1,host2,host3")

    >>> print(await alluxio.listdir("s3://mybucket/mypath/dir"))
    [
        {
            "mType": "file",
            "mName": "myfile",
            "mLength": 77542
        }

    ]
    >>> print(await alluxio.read("s3://mybucket/mypath/dir/myfile"))
    my_file_content
    """

    def __init__(
        self,
        etcd_hosts=None,
        worker_hosts=None,
        options=None,
        http_port="28080",
        etcd_port="2379",
        loop=None,
    ):
        """
        Inits Alluxio file system.

        Args:
            etcd_hosts (str, optional):
                The hostnames of ETCD to get worker addresses from
                The hostnames in host1,host2,host3 format. Either etcd_hosts or worker_hosts should be provided, not both.
            worker_hosts (str, optional):
                The worker hostnames in host1,host2,host3 format. Either etcd_hosts or worker_hosts should be provided, not both.
            options (dict, optional):
                A dictionary of Alluxio property key and values.
                Note that Alluxio Python API only support a limited set of Alluxio properties.
            etcd_port (str, optional):
                The port of each etcd server.
            http_port (string, optional):
                The port of the HTTP server on each Alluxio worker node.
        """
        if etcd_hosts is None and worker_hosts is None:
            raise ValueError(
                "Must supply either 'etcd_hosts' or 'worker_hosts'"
            )
        if etcd_hosts and worker_hosts:
            raise ValueError(
                "Supply either 'etcd_hosts' or 'worker_hosts', not both"
            )
        self._session = None

        # parse options
        page_size = ALLUXIO_PAGE_SIZE_DEFAULT_VALUE
        if options:
            if ALLUXIO_PAGE_SIZE_KEY in options:
                page_size = options[ALLUXIO_PAGE_SIZE_KEY]
                logger.debug(f"Page size is set to {page_size}")
        self.page_size = humanfriendly.parse_size(page_size, binary=True)
        self.hash_provider = ConsistentHashProvider(
            AlluxioClientConfig(
                etcd_hosts=etcd_hosts,
                etcd_port=int(etcd_port),
                worker_hosts=worker_hosts,
                worker_http_port=int(http_port),
                hash_node_per_worker=ALLUXIO_HASH_NODE_PER_WORKER_DEFAULT_VALUE,
                etcd_refresh_workers_interval=120,
            )
        )
        self.http_port = http_port
        self._loop = loop or asyncio.get_event_loop()

    async def _set_session(self):
        if self._session is None:
            self._session = aiohttp.ClientSession(loop=self._loop)
            weakref.finalize(
                self, self.close_session, self._loop, self._session
            )
        return self._session

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise RuntimeError("Please await _connect* before anything else")
        return self._session

    @staticmethod
    def close_session(loop, session):
        if loop is not None and session is not None:
            if loop.is_running():
                try:
                    loop = asyncio.get_event_loop()
                    loop.create_task(session.close())
                    return
                except RuntimeError:
                    pass
            else:
                pass

    async def listdir(self, path: str):
        """
        Lists the directory.

        Args:
            path (str): The full ufs path to list from

        Returns:
            list of dict: A list containing dictionaries, where each dictionary has:
                - mType (string): directory or file
                - mName (string): name of the directory/file
                - mLength (integer): length of the file or 0 for directory

        Example:
            [
                {
                    type: "file",
                    name: "my_file_name",
                    path: '/my_file_name',
                    ufs_path: 's3://example-bucket/my_file_name',
                    last_modification_time_ms: 0,
                    length: 77542,
                    human_readable_file_size: '75.72KB'
                },
                {
                    type: "directory",
                    name: "my_dir_name",
                    path: '/my_dir_name',
                    ufs_path: 's3://example-bucket/my_dir_name',
                    last_modification_time_ms: 0,
                    length: 0,
                    human_readable_file_size: '0B'
                },

            ]
        """
        self._validate_path(path)
        worker_host = self._get_preferred_worker_host(path)
        params = {"path": path}

        _, content = await self._request(
            Method.GET,
            LIST_URL_FORMAT.format(
                worker_host=worker_host, http_port=self.http_port
            ),
            params=params,
        )

        result = []
        for data in json.loads(content):
            result.append(
                AlluxioPathStatus(
                    data["mType"],
                    data["mName"],
                    data["mPath"],
                    data["mUfsPath"],
                    data["mLastModificationTimeMs"],
                    data["mHumanReadableFileSize"],
                    data["mLength"],
                )
            )
        return result

    async def get_file_status(self, path):
        """
        Gets the file status of the path.

        Args:
            path (str): The full ufs path to get the file status of

        Returns:
            File Status: The struct has:
                - type (string): directory or file
                - name (string): name of the directory/file
                - path (string): the path of the file
                - ufs_path (string): the ufs path of the file
                - last_modification_time_ms (long): the last modification time
                - length (integer): length of the file or 0 for directory
                - human_readable_file_size (string): the size of the human readable files

        Example:
            {
                type: 'directory',
                name: 'a',
                path: '/a',
                ufs_path: 's3://example-bucket/a',
                last_modification_time_ms: 0,
                length: 0,
                human_readable_file_size: '0B'
            }
        """
        self._validate_path(path)
        worker_host = self._get_preferred_worker_host(path)
        params = {"path": path}
        _, content = await self._request(
            Method.GET,
            GET_FILE_STATUS_URL_FORMAT.format(
                worker_host=worker_host,
                http_port=self.http_port,
            ),
            params=params,
        )
        data = json.loads(content)[0]
        return AlluxioPathStatus(
            data["mType"],
            data["mName"],
            data["mPath"],
            data["mUfsPath"],
            data["mLastModificationTimeMs"],
            data["mHumanReadableFileSize"],
            data["mLength"],
        )

    async def load(
        self,
        path: str,
        timeout=None,
    ):
        """
        Loads a file.

        Args:
            path (str): The full path with storage protocol to load data from
            timeout (integer): The number of seconds for timeout, optional

        Returns:
            result (boolean): Whether the file has been loaded successfully
        """
        self._validate_path(path)
        worker_host = self._get_preferred_worker_host(path)
        return self._load_file(worker_host, path, timeout)

    async def read_range(
        self, file_path: str, offset: int, length: int
    ) -> bytes:
        """
        Reads parts of a file.

        Args:
            file_path (str): The full ufs file path to read data from
            offset (integer): The offset to start reading data from
            length (integer): The file length to read

        Returns:
            file content (str): The file content with length from offset
        """
        self._validate_path(file_path)
        if not isinstance(offset, int) or offset < 0:
            raise ValueError("Offset must be a non-negative integer")

        if not isinstance(length, int) or (length <= 0 and length != -1):
            raise ValueError("Length must be a positive integer or -1")

        worker_host = self._get_preferred_worker_host(file_path)
        path_id = self._get_path_hash(file_path)
        page_contents = await self._range_page_generator(
            worker_host, path_id, file_path, offset, length
        )
        return b"".join(await page_contents)

    async def write_page(
        self, file_path: str, page_index: int, page_bytes: bytes
    ):
        """
        Writes a page.

        Args:
            file_path: The path of the file where data is to be written.
            page_index: The page index in the file to write the data.
            page_bytes: The byte data to write to the specified page, MUST BE FULL PAGE.

        Returns:
            True if the write was successful, False otherwise.
        """
        self._validate_path(file_path)
        worker_host = self._get_preferred_worker_host(file_path)
        path_id = self._get_path_hash(file_path)
        status, content = await self._request(
            Method.POST,
            WRITE_PAGE_URL_FORMAT.format(
                worker_host=worker_host,
                http_port=self.http_port,
                path_id=path_id,
                file_path=file_path,
                page_index=page_index,
            ),
            headers={"Content-Type": "application/octet-stream"},
            data=page_bytes,
        )
        return 200 <= status < 300

    async def _range_page_generator(
        self,
        worker_host: str,
        path_id: str,
        file_path: str,
        offset: float,
        length: float,
    ):
        start_page_index = offset // self.page_size
        start_page_offset = offset % self.page_size

        # Determine the end page index and the read-to position
        if length == -1:
            end_page_index = None
        else:
            end_page_index = (offset + length - 1) // self.page_size
            end_page_read_to = ((offset + length - 1) % self.page_size) + 1

        page_index = start_page_index
        page_contents = []
        while True:
            if page_index == start_page_index:
                if start_page_index == end_page_index:
                    read_length = end_page_read_to - start_page_offset
                else:
                    read_length = self.page_size - start_page_offset
                page_content = self._read_page(
                    worker_host,
                    path_id,
                    file_path,
                    page_index,
                    start_page_offset,
                    read_length,
                )
                page_contents.append(page_content)
            elif page_index == end_page_index:
                page_content = self._read_page(
                    worker_host,
                    path_id,
                    file_path,
                    page_index,
                    0,
                    end_page_read_to,
                )
                page_contents.append(page_content)
            else:
                page_content = self._read_page(
                    worker_host, path_id, file_path, page_index
                )
                page_contents.append(page_content)

            # Check if it's the last page or the end of the file
            if (
                page_index == end_page_index
                or len(page_content) < self.page_size
            ):
                break

            page_index += 1
        return asyncio.gather(*page_contents)

    async def _load_file(self, worker_host: str, path: str, timeout):
        _, content = await self._request(
            Method.GET,
            LOAD_SUBMIT_URL_FORMAT.format(
                worker_host=worker_host,
                http_port=self.http_port,
                path=path,
            ),
        )

        content = json.loads(content.decode("utf-8"))
        if not content[ALLUXIO_SUCCESS_IDENTIFIER]:
            return False

        load_progress_url = LOAD_PROGRESS_URL_FORMAT.format(
            worker_host=worker_host,
            http_port=self.http_port,
            path=path,
        )
        stop_time = 0
        if timeout is not None:
            stop_time = time.time() + timeout
        while True:
            job_state = await self._load_progress_internal(load_progress_url)
            if job_state == LoadState.SUCCEEDED:
                return True
            if job_state == LoadState.FAILED:
                logger.debug(
                    f"Failed to load path {path} with return message {content}"
                )
                return False
            if job_state == LoadState.STOPPED:
                logger.debug(
                    f"Failed to load path {path} with return message {content}, load stopped"
                )
                return False
            if timeout is None or stop_time - time.time() >= 10:
                asyncio.sleep(10)
            else:
                logger.debug(f"Failed to load path {path} within timeout")
                return False

    async def _load_progress_internal(self, load_url: str):
        _, content = await self._request(Method.GET, load_url)
        content = json.loads(content.decode("utf-8"))
        if "jobState" not in content:
            raise KeyError(
                "The field 'jobState' is missing from the load progress response content"
            )
        return LoadState(content["jobState"])

    async def _read_page(
        self,
        worker_host,
        path_id: str,
        file_path: str,
        page_index: int,
        offset=None,
        length=None,
    ):
        if (offset is None) != (length is None):
            raise ValueError(
                "Both offset and length should be either None or both not None"
            )

        if offset is None:
            page_url = FULL_PAGE_URL_FORMAT.format(
                worker_host=worker_host,
                http_port=self.http_port,
                path_id=path_id,
                file_path=file_path,
                page_index=page_index,
            )
        else:
            page_url = PAGE_URL_FORMAT.format(
                worker_host=worker_host,
                http_port=self.http_port,
                path_id=path_id,
                file_path=file_path,
                page_index=page_index,
                page_offset=offset,
                page_length=length,
            )

        _, content = await self._request(Method.GET, page_url)
        return content

    def _get_path_hash(self, uri: str):
        hash_functions = [
            hashlib.sha256,
            hashlib.md5,
            lambda x: hex(hash(x))[2:].lower(),  # Fallback to simple hashCode
        ]
        for hash_function in hash_functions:
            try:
                hash_obj = hash_function()
                hash_obj.update(uri.encode("utf-8"))
                return hash_obj.hexdigest().lower()
            except AttributeError:
                continue

    def _get_preferred_worker_host(self, full_ufs_path: str):
        workers = self.hash_provider.get_multiple_workers(full_ufs_path, 1)
        if len(workers) != 1:
            raise ValueError(
                "Expected exactly one worker from hash ring, but found {} workers {}.".format(
                    len(workers), workers
                )
            )
        return workers[0].host

    def _validate_path(self, path: str):
        if not isinstance(path, str):
            raise TypeError("path must be a string")

        if not re.search(r"^[a-zA-Z0-9]+://", path):
            raise ValueError(
                "path must be a full path with a protocol (e.g., 'protocol://path')"
            )

    async def _request(
        self,
        method: Method,
        url: str,
        *args,
        params: dict = None,
        headers=None,
        json=None,
        data=None,
    ) -> Tuple[int, bytes]:
        await self._set_session()
        async with self.session.request(
            method=method.value,
            url=url,
            params=params,
            json=json,
            headers=headers,
            data=data,
            timeout=None,
        ) as r:
            status = r.status
            contents = await r.read()
            # validate_response(status, contents, url, args)
            return status, contents
