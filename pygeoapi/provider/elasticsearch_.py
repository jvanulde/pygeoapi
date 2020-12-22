# =================================================================
#
# Authors: Tom Kralidis <tomkralidis@gmail.com>
#
# Copyright (c) 2020 Tom Kralidis
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation
# files (the "Software"), to deal in the Software without
# restriction, including without limitation the rights to use,
# copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the
# Software is furnished to do so, subject to the following
# conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES
# OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
# HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
# WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
# FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
# OTHER DEALINGS IN THE SOFTWARE.
#
# =================================================================

from collections import OrderedDict
import json
import logging

from elasticsearch import Elasticsearch, exceptions, helpers
from elasticsearch.client.indices import IndicesClient

from pygeoapi.provider.base import (BaseProvider, ProviderConnectionError,
                                    ProviderQueryError,
                                    ProviderItemNotFoundError)

LOGGER = logging.getLogger(__name__)


class ElasticsearchProvider(BaseProvider):
    """Elasticsearch Provider"""

    def __init__(self, provider_def):
        """
        Initialize object

        :param provider_def: provider definition

        :returns: pygeoapi.provider.elasticsearch_.ElasticsearchProvider
        """

        super().__init__(provider_def)

        url_tokens = self.data.split('/')

        LOGGER.debug('Setting Elasticsearch properties')
        self.es_host = url_tokens[2]
        self.index_name = url_tokens[-1]
        self.is_gdal = False

        LOGGER.debug('host: {}'.format(self.es_host))
        LOGGER.debug('index: {}'.format(self.index_name))

        LOGGER.debug('Connecting to Elasticsearch')
        self.es = Elasticsearch(self.es_host)
        if not self.es.ping():
            msg = 'Cannot connect to Elasticsearch'
            LOGGER.error(msg)
            raise ProviderConnectionError(msg)

        LOGGER.debug('Determining ES version')
        v = self.es.info()['version']['number'][:3]
        if float(v) < 7:
            msg = 'only ES 7+ supported'
            LOGGER.error(msg)
            raise ProviderConnectionError(msg)

        LOGGER.debug('Grabbing field information')
        try:
            self.fields = self.get_fields()
        except exceptions.NotFoundError as err:
            LOGGER.error(err)
            raise ProviderQueryError(err)

    def get_fields(self):
        """
         Get provider field information (names, types)

        :returns: dict of fields
        """

        fields_ = {}
        ic = IndicesClient(self.es)
        ii = ic.get(self.index_name)

        try:
            p = ii[self.index_name]['mappings']['properties']['properties']  # noqa
        except KeyError:
            LOGGER.debug('ES index looks generated by GDAL')
            self.is_gdal = True
            p = ii[self.index_name]['mappings']

        for k, v in p['properties'].items():
            if 'type' in v:
                if v['type'] == 'text':
                    type_ = 'string'
                else:
                    type_ = v['type']
                fields_[k] = type_

        return fields_

    def query(self, startindex=0, limit=10, resulttype='results',
              bbox=[], datetime_=None, properties=[], sortby=[],
              select_properties=[], skip_geometry=False):
        """
        query Elasticsearch index

        :param startindex: starting record to return (default 0)
        :param limit: number of records to return (default 10)
        :param resulttype: return results or hit limit (default results)
        :param bbox: bounding box [minx,miny,maxx,maxy]
        :param datetime_: temporal (datestamp or extent)
        :param properties: list of tuples (name, value)
        :param sortby: list of dicts (property, order)
        :param select_properties: list of property names
        :param skip_geometry: bool of whether to skip geometry (default False)

        :returns: dict of 0..n GeoJSON features
        """

        query = {'track_total_hits': True, 'query': {'bool': {'filter': []}}}
        filter_ = []

        feature_collection = {
            'type': 'FeatureCollection',
            'features': []
        }

        if resulttype == 'hits':
            LOGGER.debug('hits only specified')
            limit = 0

        if bbox:
            LOGGER.debug('processing bbox parameter')
            minx, miny, maxx, maxy = bbox
            bbox_filter = {
                'geo_shape': {
                    'geometry': {
                        'shape': {
                            'type': 'envelope',
                            'coordinates': [[minx, maxy], [maxx, miny]]
                        },
                        'relation': 'intersects'
                    }
                }
            }

            query['query']['bool']['filter'].append(bbox_filter)

        if datetime_ is not None:
            LOGGER.debug('processing datetime parameter')
            if self.time_field is None:
                LOGGER.error('time_field not enabled for collection')
                raise ProviderQueryError()

            time_field = self.mask_prop(self.time_field)

            if '/' in datetime_:  # envelope
                LOGGER.debug('detected time range')
                time_begin, time_end = datetime_.split('/')

                range_ = {
                    'range': {
                        time_field: {
                            'gte': time_begin,
                            'lte': time_end
                        }
                    }
                }
                if time_begin == '..':
                    range_['range'][time_field].pop('gte')
                elif time_end == '..':
                    range_['range'][time_field].pop('lte')

                filter_.append(range_)

            else:  # time instant
                LOGGER.debug('detected time instant')
                filter_.append({'match': {time_field: datetime_}})

            LOGGER.debug(filter_)
            query['query']['bool']['filter'].append(*filter_)

        if properties:
            LOGGER.debug('processing properties')
            for prop in properties:
                pf = {
                    'match': {
                        self.mask_prop(prop[0]): prop[1]
                    }
                }
                query['query']['bool']['filter'].append(pf)

        if sortby:
            LOGGER.debug('processing sortby')
            query['sort'] = []
            for sort in sortby:
                LOGGER.debug('processing sort object: {}'.format(sort))

                sp = sort['property']

                if self.fields[sp] == 'string':
                    LOGGER.debug('setting ES .raw on property')
                    sort_property = '{}.raw'.format(self.mask_prop(sp))
                else:
                    sort_property = self.mask_prop(sp)

                sort_order = 'asc'
                if sort['order'] == 'D':
                    sort_order = 'desc'

                sort_ = {
                    sort_property: {
                        'order': sort_order
                    }
                }
                query['sort'].append(sort_)

        if self.properties or select_properties:
            LOGGER.debug('including specified fields: {}'.format(
                self.properties))
            query['_source'] = {
                'includes': list(map(self.mask_prop,
                                 set(self.properties) | set(select_properties)))  # noqa
            }
            query['_source']['includes'].append(self.mask_prop(self.id_field))
            query['_source']['includes'].append('type')
            query['_source']['includes'].append('geometry')
        if skip_geometry:
            LOGGER.debug('limiting to specified fields: {}'.format(
                select_properties))
            try:
                query['_source']['excludes'] = ['geometry']
            except KeyError:
                query['_source'] = {'excludes': ['geometry']}
        try:
            LOGGER.debug('querying Elasticsearch')
            LOGGER.debug(json.dumps(query, indent=4))

            LOGGER.debug('Setting ES paging zero-based')
            if startindex > 0:
                startindex2 = startindex - 1
            else:
                startindex2 = startindex

            if startindex2 + limit > 10000:
                gen = helpers.scan(client=self.es, query=query,
                                   preserve_order=True,
                                   index=self.index_name)
                results = {'hits': {'total': limit, 'hits': []}}
                for i in range(startindex2 + limit):
                    try:
                        if i >= startindex2:
                            results['hits']['hits'].append(next(gen))
                        else:
                            next(gen)
                    except StopIteration:
                        break
                results['hits']['total'] = \
                    len(results['hits']['hits']) + startindex2
            else:
                results = self.es.search(index=self.index_name,
                                         from_=startindex2, size=limit,
                                         body=query)
                results['hits']['total'] = results['hits']['total']['value']

        except exceptions.ConnectionError as err:
            LOGGER.error(err)
            raise ProviderConnectionError()
        except exceptions.RequestError as err:
            LOGGER.error(err)
            raise ProviderQueryError()
        except exceptions.NotFoundError as err:
            LOGGER.error(err)
            raise ProviderQueryError()

        feature_collection['numberMatched'] = results['hits']['total']

        if resulttype == 'hits':
            return feature_collection

        feature_collection['numberReturned'] = len(results['hits']['hits'])

        LOGGER.debug('serializing features')
        for feature in results['hits']['hits']:
            feature_ = self.esdoc2geojson(feature)
            feature_collection['features'].append(feature_)

        return feature_collection

    def get(self, identifier):
        """
        Get ES document by id

        :param identifier: feature id

        :returns: dict of single GeoJSON feature
        """

        try:
            LOGGER.debug('Fetching identifier {}'.format(identifier))
            result = self.es.get(self.index_name, id=identifier)
            LOGGER.debug('Serializing feature')
            feature_ = self.esdoc2geojson(result)
        except exceptions.NotFoundError as err:
            LOGGER.debug('Not found via ES id query: {}'.format(err))
            LOGGER.debug('Trying via a real query')

            query = {
                'query': {
                    'bool': {
                        'filter': [{
                            'match': {
                                self.id_field: identifier
                            }
                        }]
                    }
                }
            }

            result = self.es.search(index=self.index_name, body=query)
            if len(result['hits']['hits']) == 0:
                LOGGER.error(err)
                raise ProviderItemNotFoundError(err)
            LOGGER.debug('Serializing feature')
            feature_ = self.esdoc2geojson(result['hits']['hits'][0])
        except Exception as err:
            LOGGER.error(err)
            return None

        return feature_

    def esdoc2geojson(self, doc):
        """
        generate GeoJSON `dict` from ES document

        :param doc: `dict` of ES document

        :returns: GeoJSON `dict`
        """

        feature_ = {}
        feature_thinned = {}

        if 'properties' not in doc['_source']:
            LOGGER.debug('Looks like a GDAL ES 7 document')
            id_ = doc['_source'][self.id_field]
            if 'type' not in doc['_source']:
                feature_['id'] = id_
                feature_['type'] = 'Feature'
            feature_['geometry'] = doc['_source'].get('geometry')
            feature_['properties'] = {}
            for key, value in doc['_source'].items():
                if key == 'geometry':
                    continue
                feature_['properties'][key] = value
        else:
            LOGGER.debug('Looks like true GeoJSON document')
            feature_ = doc['_source']
            id_ = doc['_source']['properties'][self.id_field]
            feature_['id'] = id_
            feature_['geometry'] = doc['_source'].get('geometry')

        if self.properties:
            feature_thinned = {
                'id': id_,
                'type': feature_['type'],
                'geometry': feature_.get('geometry'),
                'properties': OrderedDict()
            }
            for p in self.properties:
                try:
                    feature_thinned['properties'][p] = feature_['properties'][p]  # noqa
                except KeyError as err:
                    LOGGER.error(err)
                    raise ProviderQueryError()

        if feature_thinned:
            return feature_thinned
        else:
            return feature_

    def mask_prop(self, property_name):
        """
        generate property name based on ES backend setup

        :param property_name: property name

        :returns: masked property name
        """

        if self.is_gdal:
            return property_name
        else:
            return 'properties.{}'.format(property_name)

    def __repr__(self):
        return '<ElasticsearchProvider> {}'.format(self.data)
