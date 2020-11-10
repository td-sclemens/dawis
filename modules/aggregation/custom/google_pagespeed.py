from database.connection import Connection
from utilities.configuration import Configuration
from utilities.exceptions import ConfigurationInvalidError, ConfigurationMissingError
from utilities.thread import ResultThread
from utilities.validator import Validator
from google.cloud.bigquery.job import LoadJobConfig, WriteDisposition
from google.cloud.bigquery.enums import SqlTypeNames
from google.cloud.bigquery.schema import SchemaField
from google.cloud.bigquery.table import TableReference, TimePartitioning, TimePartitioningType
from googleapiclient.errors import HttpError
from googleapiclient.discovery import build
from googleapiclient.http import HttpRequest
from datetime import datetime, timedelta
from time import time, sleep
from typing import Sequence
import dateutil
import re


class GooglePagespeed:
    COLLECTION_NAME = 'google_pagespeed'

    STRATEGIES_ALLOWED = ['desktop', 'mobile', 'both']
    MAX_PARALLEL_REQUESTS = 10
    SECONDS_BETWEEN_REQUESTS = 3

    def __init__(self, configuration: Configuration, configuration_key: str, connection: Connection):
        self.configuration = configuration
        self.module_configuration = configuration.aggregations.get_custom_configuration_aggregation(configuration_key)
        self.connection = connection
        self.mongodb = None
        self.bigquery = None

    def run(self):
        print('Running Google Pagespeed Module:')
        timer_run = time()
        api_key = None

        if 'bigquery' == self.module_configuration.database:
            self.bigquery = self.connection.bigquery
        else:
            self.mongodb = self.connection.mongodb

        if 'apiKey' in self.module_configuration.settings and type(self.module_configuration.settings['apiKey']) is str:
            api_key = self.module_configuration.settings['apiKey']

        if 'configurations' in self.module_configuration.settings and \
                type(self.module_configuration.settings['configurations']) is list:
            for configuration in self.module_configuration.settings['configurations']:
                self._process_configuration(configuration, api_key, self.module_configuration.database)

        print('\ncompleted: {:s}'.format(str(timedelta(seconds=int(time() - timer_run)))))

    def _process_configuration(self, configuration: dict, api_key: str, database: str):
        strategies = ['DESKTOP']
        table_reference = None
        log_table_reference = None

        if 'bigquery' == database:
            if 'tablename' in configuration and type(configuration['tablename']) is str:
                table_name = configuration['tablename']
            else:
                raise ConfigurationMissingError('Missing tablename for pagespeed to bigquery')

            dataset_name = None

            if 'dataset' in configuration and type(configuration['dataset']) is str:
                dataset_name = configuration['dataset']

            table_reference = self.connection.bigquery.table_reference(table_name, dataset_name)

            if 'logTablename' in configuration and type(configuration['logTablename']) is str:
                log_table_reference = self.connection.bigquery.table_reference(
                    configuration['logTablename'],
                    dataset_name
                )

        cluster = {}

        if 'cluster' in configuration and type(configuration['cluster']) is dict:
            for cluster_name, urls in configuration['cluster'].items():
                for url in urls:
                    if type(url) is not str:
                        raise ConfigurationInvalidError('Invalid url')
                    elif not Validator.validate_url(url):
                        raise ConfigurationInvalidError('Invalid url')

                cluster[cluster_name] = urls

        if 'strategy' in configuration and type(configuration['strategy']) is str:
            if configuration['strategy'] in self.STRATEGIES_ALLOWED:
                if 'both' == configuration['strategy']:
                    strategies = ['DESKTOP', 'MOBILE']
                else:
                    strategies = [str.upper(configuration['strategy'])]
            else:
                raise ConfigurationInvalidError('invalid strategy for pagespeed')

        if 'apiKey' in configuration and type(configuration['apiKey']) is str:
            api_key = configuration['apiKey']

        requests = []
        responses = []
        log = []

        for cluster_name, urls in cluster.items():
            for url in urls:
                for strategy in strategies:
                    requests.append([url, cluster_name, strategy, api_key])

        responses, failed_requests, log = self._process_requests(requests, responses, log)

        if 0 < len(failed_requests):
            responses, failed_requests, log = self._process_requests(failed_requests, responses, log)

        if 0 < len(failed_requests):
            for failed_request in failed_requests:
                print('Failed API request for URL: "{r[0]}", strategy: "{r[2]}"'.format(r=failed_request))

        if 'bigquery' == database:
            self._process_responses_for_bigquery(responses, table_reference)

            if type(log_table_reference) is TableReference:
                self._process_log_for_bigquery(log, log_table_reference)
        else:
            self._process_responses_for_mongodb(responses)

    def _process_requests(self, requests: list, responses: list, log: list) -> tuple:
        status_code_regex = re.compile(r'status[\s\-_]code:?\s?(\d+)', re.IGNORECASE)
        failed_requests = []

        requests_chunks = [
            requests[i:i + GooglePagespeed.MAX_PARALLEL_REQUESTS]
            for i in range(0, len(requests), GooglePagespeed.MAX_PARALLEL_REQUESTS)
        ]

        for requests_chunk in requests_chunks:
            threads = []

            for request in requests_chunk:
                thread = ResultThread(self._process_pagespeed_api, request)
                thread.start()
                threads.append(thread)

            for thread in threads:
                thread.join()
                request = thread.get_arguements()

                if isinstance(thread.exception, Exception) or type(thread.result) is not dict:
                    status_code = None

                    if type(thread.exception) is HttpError:
                        match = status_code_regex.search(thread.exception.__str__())

                        if type(match) is re.Match:
                            status_code = int(match.group(1))

                    log.append({
                        'url': request[0],
                        'cluster': request[1],
                        'strategy': request[2],
                        'date': datetime.utcnow(),
                        'statusCode': status_code,
                        'message': thread.exception.__str__()
                    })

                    failed_requests.append(request)
                else:
                    response = thread.result
                    responses.append(response)

                    log.append({
                        'url': request[0],
                        'cluster': request[1],
                        'strategy': request[2],
                        'date': datetime.utcnow(),
                        'statusCode': response['statusCode'],
                        'message': None
                    })

            if len(requests_chunks) != requests_chunks.index(requests_chunk) + 1:
                sleep(GooglePagespeed.SECONDS_BETWEEN_REQUESTS)

        return responses, failed_requests, log

    def _process_pagespeed_api(
            self,
            url: str,
            cluster: str,
            strategy: str,
            api_key: str
    ) -> dict:
        pagespeed_api = build(
            'pagespeedonline',
            'v5',
            developerKey=api_key,
            cache_discovery=False
        ).pagespeedapi()

        request: HttpRequest = pagespeed_api.runpagespeed(url=url, strategy=strategy)
        return self._process_response(request.execute(), url, cluster, strategy)

    def _process_responses_for_mongodb(self, responses: Sequence[dict]):
        self.mongodb.insert_documents(GooglePagespeed.COLLECTION_NAME, responses)

    def _process_responses_for_bigquery(self, data: Sequence[dict], table_reference: TableReference):
        job_config = LoadJobConfig()
        job_config.write_disposition = WriteDisposition.WRITE_APPEND
        job_config.time_partitioning = TimePartitioning(type_=TimePartitioningType.DAY, field='date')

        loading_experience_schema_fields = (
            SchemaField('cls', SqlTypeNames.INTEGER, 'REQUIRED'),
            SchemaField('clsGood', SqlTypeNames.FLOAT, 'REQUIRED'),
            SchemaField('clsMedium', SqlTypeNames.FLOAT, 'REQUIRED'),
            SchemaField('clsBad', SqlTypeNames.FLOAT, 'REQUIRED'),
            SchemaField('lcp', SqlTypeNames.INTEGER, 'REQUIRED'),
            SchemaField('lcpGood', SqlTypeNames.FLOAT, 'REQUIRED'),
            SchemaField('lcpMedium', SqlTypeNames.FLOAT, 'REQUIRED'),
            SchemaField('lcpBad', SqlTypeNames.FLOAT, 'REQUIRED'),
            SchemaField('fcp', SqlTypeNames.INTEGER, 'REQUIRED'),
            SchemaField('fcpGood', SqlTypeNames.FLOAT, 'REQUIRED'),
            SchemaField('fcpMedium', SqlTypeNames.FLOAT, 'REQUIRED'),
            SchemaField('fcpBad', SqlTypeNames.FLOAT, 'REQUIRED'),
            SchemaField('fid', SqlTypeNames.INTEGER, 'REQUIRED'),
            SchemaField('fidGood', SqlTypeNames.FLOAT, 'REQUIRED'),
            SchemaField('fidMedium', SqlTypeNames.FLOAT, 'REQUIRED'),
            SchemaField('fidBad', SqlTypeNames.FLOAT, 'REQUIRED'),
        )

        job_config.schema = (
            SchemaField('url', SqlTypeNames.STRING, 'REQUIRED'),
            SchemaField('strategy', SqlTypeNames.STRING, 'REQUIRED'),
            SchemaField('date', SqlTypeNames.DATETIME, 'REQUIRED'),
            SchemaField('statusCode', SqlTypeNames.INTEGER, 'REQUIRED'),
            SchemaField('cluster', SqlTypeNames.STRING, 'REQUIRED'),
            SchemaField('labdata', SqlTypeNames.RECORD, 'REQUIRED', fields=(
                SchemaField('cls', SqlTypeNames.FLOAT, 'REQUIRED'),
                SchemaField('lcp', SqlTypeNames.FLOAT, 'REQUIRED'),
                SchemaField('fcp', SqlTypeNames.FLOAT, 'REQUIRED'),
                SchemaField('tbt', SqlTypeNames.FLOAT, 'REQUIRED'),
                SchemaField('mpfid', SqlTypeNames.FLOAT, 'REQUIRED'),
                SchemaField('ttfb', SqlTypeNames.FLOAT, 'REQUIRED'),
                SchemaField('performanceScore', SqlTypeNames.FLOAT, 'REQUIRED'),
                SchemaField('serverResponseTime', SqlTypeNames.FLOAT, 'REQUIRED'),
                SchemaField('usesTextCompression', SqlTypeNames.FLOAT, 'REQUIRED'),
                SchemaField('usesLongCacheTtl', SqlTypeNames.FLOAT, 'REQUIRED'),
                SchemaField('domSize', SqlTypeNames.FLOAT, 'REQUIRED'),
                SchemaField('offscreenImages', SqlTypeNames.FLOAT, 'REQUIRED'),
                SchemaField('usesOptimizedImages', SqlTypeNames.FLOAT, 'REQUIRED'),
                SchemaField('usesResponsiveImages', SqlTypeNames.FLOAT, 'REQUIRED'),
                SchemaField('renderBlockingResources', SqlTypeNames.FLOAT, 'REQUIRED'),
                SchemaField('bootupTime', SqlTypeNames.FLOAT, 'REQUIRED'),
                SchemaField('mainthreadWorkBreakdown', SqlTypeNames.FLOAT, 'REQUIRED'),
            )),
            SchemaField(
                'originLoadingExperience',
                SqlTypeNames.RECORD,
                'REQUIRED',
                fields=loading_experience_schema_fields
            ),
            SchemaField('loadingExperience', SqlTypeNames.RECORD, fields=loading_experience_schema_fields)
        )

        for data_item in data:
            data_item['date'] = data_item['date'].strftime('%Y-%m-%dT%H:%M:%S.%f')

        load_job = self.bigquery.client.load_table_from_json(data, table_reference, job_config=job_config)
        load_job.result()

    def _process_log_for_bigquery(self, log: Sequence[dict], table_reference: TableReference):
        job_config = LoadJobConfig()
        job_config.write_disposition = WriteDisposition.WRITE_APPEND
        job_config.time_partitioning = TimePartitioning(type_=TimePartitioningType.DAY, field='date')

        job_config.schema = (
            SchemaField('url', SqlTypeNames.STRING, 'REQUIRED'),
            SchemaField('cluster', SqlTypeNames.STRING, 'REQUIRED'),
            SchemaField('strategy', SqlTypeNames.STRING, 'REQUIRED'),
            SchemaField('date', SqlTypeNames.DATETIME, 'REQUIRED'),
            SchemaField('statusCode', SqlTypeNames.INTEGER),
            SchemaField('message', SqlTypeNames.STRING),
        )

        for log_item in log:
            log_item['date'] = log_item['date'].strftime('%Y-%m-%dT%H:%M:%S.%f')

        load_job = self.bigquery.client.load_table_from_json(log, table_reference, job_config=job_config)
        load_job.result()

    @staticmethod
    def _process_response(response: dict, url: str, cluster: str, strategy: str) -> dict:
        loading_experience_dummy = lambda x: {
            'cls': response[x]['metrics']['CUMULATIVE_LAYOUT_SHIFT_SCORE']['percentile'],
            'clsGood': response[x]['metrics']['CUMULATIVE_LAYOUT_SHIFT_SCORE']['distributions'][0]['proportion'],
            'clsMedium': response[x]['metrics']['CUMULATIVE_LAYOUT_SHIFT_SCORE']['distributions'][1]['proportion'],
            'clsBad': response[x]['metrics']['CUMULATIVE_LAYOUT_SHIFT_SCORE']['distributions'][2]['proportion'],
            'lcp': response[x]['metrics']['LARGEST_CONTENTFUL_PAINT_MS']['percentile'],
            'lcpGood': response[x]['metrics']['LARGEST_CONTENTFUL_PAINT_MS']['distributions'][0]['proportion'],
            'lcpMedium': response[x]['metrics']['LARGEST_CONTENTFUL_PAINT_MS']['distributions'][1]['proportion'],
            'lcpBad': response[x]['metrics']['LARGEST_CONTENTFUL_PAINT_MS']['distributions'][2]['proportion'],
            'fcp': response['originLoadingExperience']['metrics']['FIRST_CONTENTFUL_PAINT_MS']['percentile'],
            'fcpGood': response[x]['metrics']['FIRST_CONTENTFUL_PAINT_MS']['distributions'][0]['proportion'],
            'fcpMedium': response[x]['metrics']['FIRST_CONTENTFUL_PAINT_MS']['distributions'][1]['proportion'],
            'fcpBad': response[x]['metrics']['FIRST_CONTENTFUL_PAINT_MS']['distributions'][2]['proportion'],
            'fid': response['originLoadingExperience']['metrics']['FIRST_INPUT_DELAY_MS']['percentile'],
            'fidGood': response[x]['metrics']['FIRST_INPUT_DELAY_MS']['distributions'][0]['proportion'],
            'fidMedium': response[x]['metrics']['FIRST_INPUT_DELAY_MS']['distributions'][1]['proportion'],
            'fidBad': response[x]['metrics']['FIRST_INPUT_DELAY_MS']['distributions'][2]['proportion'],
        }

        status_code = int(
            response['lighthouseResult']['audits']['network-requests']['details']['items'][0]['statusCode']
        )

        data = {
            'url': url,
            'strategy': strategy,
            'statusCode': status_code,
            'date': dateutil.parser.parse(response['analysisUTCTimestamp']),
            'cluster': cluster,
            'labdata': {
                'cls': response['lighthouseResult']['audits']['cumulative-layout-shift']['numericValue'],
                'lcp': response['lighthouseResult']['audits']['largest-contentful-paint']['numericValue'],
                'fcp': response['lighthouseResult']['audits']['first-contentful-paint']['numericValue'],
                'tbt': response['lighthouseResult']['audits']['total-blocking-time']['numericValue'],
                'mpfid': response['lighthouseResult']['audits']['max-potential-fid']['numericValue'],
                'ttfb': response['lighthouseResult']['audits']['server-response-time']['numericValue'],
                'performanceScore': response['lighthouseResult']['categories']['performance']['score'],
                'serverResponseTime': response['lighthouseResult']['audits']['server-response-time']['score'],
                'usesTextCompression': response['lighthouseResult']['audits']['uses-text-compression']['score'],
                'usesLongCacheTtl': response['lighthouseResult']['audits']['uses-long-cache-ttl']['score'],
                'domSize': response['lighthouseResult']['audits']['dom-size']['score'],
                'offscreenImages': response['lighthouseResult']['audits']['offscreen-images']['score'],
                'usesOptimizedImages': response['lighthouseResult']['audits']['uses-optimized-images']['score'],
                'usesResponsiveImages': response['lighthouseResult']['audits']['uses-responsive-images']['score'],
                'renderBlockingResources': response['lighthouseResult']['audits']['render-blocking-resources']['score'],
                'bootupTime': response['lighthouseResult']['audits']['bootup-time']['score'],
                'mainthreadWorkBreakdown': response['lighthouseResult']['audits']['mainthread-work-breakdown']['score'],
            },
            'originLoadingExperience': loading_experience_dummy('originLoadingExperience'),
            'loadingExperience': None,
        }

        if 'loadingExperience' in response and (
                'origin_fallback' not in response['loadingExperience'] or
                response['loadingExperience']['origin_fallback'] is not True
        ):
            data['loadingExperience'] = loading_experience_dummy('loadingExperience')

        return data
