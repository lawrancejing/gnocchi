# -*- encoding: utf-8 -*-
#
# Copyright © 2014-2015 eNovance
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.
import functools
import json
import uuid

from oslo.utils import strutils
from oslo.utils import timeutils
from oslo_log import log
import pecan
from pecan import rest
from pytimeparse import timeparse
import six
from six.moves.urllib import parse as urllib_parse
from stevedore import extension
import voluptuous
import webob.exc
import werkzeug.http

from gnocchi import aggregates
from gnocchi import archive_policy
from gnocchi import indexer
from gnocchi.openstack.common import policy
from gnocchi import storage
from gnocchi import utils


LOGICAL_AND = '∧'


_ENFORCER = None

LOG = log.getLogger(__name__)


def enforce(rule, target):
    """Return the user and project the request should be limited to.

    :param rule: The rule name
    :param target: The target to enforce on.

    """
    global _ENFORCER
    if not _ENFORCER:
        _ENFORCER = policy.Enforcer()

    headers = pecan.request.headers

    # NOTE(jd) If user_id or project_id are UUID, try to convert them in the
    # proper dashed format. It's indeed possible that a middleware passes
    # theses UUID without the dash representation. It's valid, we can parse,
    # but the policy module won't see the equality in the string
    # representations.
    user_id = headers.get("X-User-Id")
    if user_id:
        try:
            user_id = six.text_type(uuid.UUID(user_id))
        except Exception:
            pecan.abort(400, "Malformed X-User-Id")

    project_id = headers.get("X-Project-Id")
    if project_id:
        try:
            project_id = six.text_type(uuid.UUID(project_id))
        except Exception:
            pecan.abort(400, "Malformed X-Project-Id")

    creds = {
        'roles': headers.get("X-Roles", "").split(","),
        'user_id': user_id,
        'project_id': project_id
    }

    if not _ENFORCER.enforce(rule, target, creds):
        pecan.abort(403)


def set_resp_location_hdr(location):
    # NOTE(sileht): according the pep-3333 the headers must be
    # str in py2 and py3 even this is not the same thing in both
    # version
    # see: http://legacy.python.org/dev/peps/pep-3333/#unicode-issues
    if six.PY2 and isinstance(location, six.text_type):
        location = location.encode('utf-8')
    location = urllib_parse.quote(location)
    pecan.response.headers['Location'] = location


def get_user_and_project():
    return (pecan.request.headers.get('X-User-Id'),
            pecan.request.headers.get('X-Project-Id'))


def deserialize(schema):
    mime_type, options = werkzeug.http.parse_options_header(
        pecan.request.headers.get('Content-Type'))
    if mime_type != "application/json":
        pecan.abort(415)
    try:
        params = json.loads(pecan.request.body.decode(
            options.get('charset', 'ascii')))
    except Exception as e:
        pecan.abort(400, "Unable to decode body: " + str(e))
    try:
        return schema(params)
    except voluptuous.Error as e:
        pecan.abort(400, "Invalid input: %s" % e)


def vexpose(schema, *vargs, **vkwargs):
    def expose(f):
        f = pecan.expose(*vargs, **vkwargs)(f)

        @functools.wraps(f)
        def callfunction(*args, **kwargs):
            return f(*args, body=deserialize(schema), **kwargs)
        return callfunction
    return expose


def Timestamp(v):
    if v is None:
        return v
    return utils.to_timestamp(v)


def convert_metric_list(metrics, created_by_user_id, created_by_project_id):
    # Replace an archive policy as value for an metric by a brand
    # a new metric
    new_metrics = {}
    for k, v in six.iteritems(metrics):
        if isinstance(v, uuid.UUID):
            new_metrics[k] = v
        else:
            new_metrics[k] = str(MetricsController.create_metric(
                created_by_user_id, created_by_project_id,
                v['archive_policy_name']))
    return new_metrics


def PositiveOrNullInt(value):
    value = int(value)
    if value < 0:
        raise ValueError("Value must be positive")
    return value


def PositiveNotNullInt(value):
    value = int(value)
    if value <= 0:
        raise ValueError("Value must be positive and not null")
    return value


def Timespan(value):
    if value is None:
        raise ValueError("Invalid timespan")
    try:
        seconds = int(value)
    except Exception:
        try:
            seconds = timeparse.timeparse(six.text_type(value))
        except Exception:
            raise ValueError("Unable to parse timespan")
    if seconds is None:
        raise ValueError("Unable to parse timespan")
    if seconds <= 0:
        raise ValueError("Timespan must be positive")
    return seconds


def get_details(params):
    type, options = werkzeug.http.parse_options_header(
        pecan.request.headers.get('Accept'))
    try:
        details = strutils.bool_from_string(
            options.get('details', params.pop('details', 'false')),
            strict=True)
    except ValueError as e:
        method = 'Accept' if 'details' in options else 'query'
        pecan.abort(
            400,
            "Unable to parse details value in %s: %s" % (method, str(e)))
    return details


def ValidAggMethod(value):
    value = six.text_type(value)
    if value in archive_policy.ArchivePolicy.VALID_AGGREGATION_METHODS_VALUES:
        return value
    raise ValueError("Invalid aggregation method")


class ArchivePoliciesController(rest.RestController):
    @pecan.expose('json')
    def post(self):
        # NOTE(jd): Initialize this one at run-time because we rely on conf
        conf = pecan.request.conf
        ArchivePolicySchema = voluptuous.Schema({
            voluptuous.Required("name"): six.text_type,
            voluptuous.Required("back_window", default=0): PositiveOrNullInt,
            voluptuous.Required(
                "aggregation_methods",
                default=set(conf.archive_policy.default_aggregation_methods)):
            [ValidAggMethod],
            voluptuous.Required("definition"):
            voluptuous.All([{
                "granularity": Timespan,
                "points": PositiveNotNullInt,
                "timespan": Timespan,
                }], voluptuous.Length(min=1)),
            })

        body = deserialize(ArchivePolicySchema)
        # Validate the data
        try:
            ap = archive_policy.ArchivePolicy.from_dict(body)
        except ValueError as e:
            pecan.abort(400, e)
        enforce("create archive policy", ap.to_dict())
        try:
            ap = pecan.request.indexer.create_archive_policy(ap)
        except indexer.ArchivePolicyAlreadyExists as e:
            pecan.abort(409, e)

        location = "/v1/archive_policy/" + ap['name']
        set_resp_location_hdr(location)
        pecan.response.status = 201
        return archive_policy.ArchivePolicy.from_dict(
            ap).to_human_readable_dict()

    @pecan.expose('json')
    def get_one(self, id):
        ap = pecan.request.indexer.get_archive_policy(id)
        if ap:
            enforce("get archive policy", ap)
            return archive_policy.ArchivePolicy.from_dict(
                ap).to_human_readable_dict()
        pecan.abort(404)

    @pecan.expose('json')
    def get_all(self):
        enforce("list archive policy", {})
        return [
            archive_policy.ArchivePolicy.from_dict(
                ap).to_human_readable_dict()
            for ap in pecan.request.indexer.list_archive_policies()
        ]

    @pecan.expose()
    def delete(self, name):
        try:
            pecan.request.indexer.delete_archive_policy(name)
        except indexer.NoSuchArchivePolicy as e:
            pecan.abort(404, e)
        except indexer.ArchivePolicyInUse as e:
            pecan.abort(400, e)


class AggregatedMetricController(rest.RestController):
    _custom_actions = {
        'measures': ['GET']
    }

    def __init__(self, metric_ids):
        self.metric_ids = metric_ids

    @pecan.expose('json')
    def get_measures(self, start=None, stop=None, aggregation='mean',
                     needed_overlap=100.0):
        return self.get_cross_metric_measures(self.metric_ids, start, stop,
                                              aggregation, needed_overlap)

    @staticmethod
    def get_cross_metric_measures(metric_ids, start=None, stop=None,
                                  aggregation='mean', needed_overlap=100.0):
        if (aggregation
           not in archive_policy.ArchivePolicy.VALID_AGGREGATION_METHODS):
            pecan.abort(
                400,
                'Invalid aggregation value %s, must be one of %s'
                % (aggregation,
                   archive_policy.ArchivePolicy.VALID_AGGREGATION_METHODS))

        # Check RBAC policy
        metrics = pecan.request.indexer.get_metrics(metric_ids)
        missing_metric_ids = (set(six.text_type(m['id']) for m in metrics)
                              - set(metric_ids))
        if missing_metric_ids:
            # Return one of the missing one in the error
            pecan.abort(404, storage.MetricDoesNotExist(
                missing_metric_ids.pop()))

        for metric in metrics:
            enforce("get metric", metric)

        try:
            if len(metric_ids) == 1:
                # NOTE(sileht): don't do the aggregation if we only have one
                # metric
                # NOTE(jd): set the archive policy to None as it's not really
                # used and it has a cost to request it from the indexer
                measures = pecan.request.storage.get_measures(
                    storage.Metric(metric_ids[0], None),
                    start, stop, aggregation)
            else:
                # NOTE(jd): set the archive policy to None as it's not really
                # used and it has a cost to request it from the indexer
                measures = pecan.request.storage.get_cross_metric_measures(
                    [storage.Metric(m, None) for m in metric_ids],
                    start, stop, aggregation, needed_overlap)
            # Replace timestamp keys by their string versions
            return [(timeutils.isotime(timestamp, subsecond=True), offset, v)
                    for timestamp, offset, v in measures]
        except storage.MetricUnaggregatable:
            pecan.abort(400, "One of the metric to aggregated doesn't have "
                        "matching granularity")
        except storage.MetricDoesNotExist as e:
            pecan.abort(404, str(e))


class MetricController(rest.RestController):
    _custom_actions = {
        'measures': ['POST', 'GET']
    }

    def __init__(self, metric_id):
        self.metric_id = metric_id
        mgr = extension.ExtensionManager(namespace='gnocchi.aggregates',
                                         invoke_on_load=True)
        self.custom_agg = dict((x.name, x.obj) for x in mgr)

    Measures = voluptuous.Schema([{
        voluptuous.Required("timestamp"):
        Timestamp,
        voluptuous.Required("value"): voluptuous.Any(float, int),
    }])

    def enforce_metric(self, rule, details=False):
        metrics = pecan.request.indexer.get_metrics((self.metric_id,),
                                                    details=details)
        if not metrics:
            pecan.abort(404, storage.MetricDoesNotExist(self.metric_id))
        enforce(rule, metrics[0])
        return metrics

    @pecan.expose('json')
    def get_all(self, **kwargs):
        details = get_details(kwargs)
        metric = self.enforce_metric("get metric", details)[0]

        if details:
            metric['archive_policy'] = (
                archive_policy.ArchivePolicy.from_dict(
                    metric['archive_policy']
                ).to_human_readable_dict())
        return metric

    @vexpose(Measures)
    def post_measures(self, body):
        metric = self.enforce_metric("post measures", details=True)[0]
        try:
            pecan.request.storage.add_measures(
                storage.Metric(
                    name=self.metric_id,
                    archive_policy=archive_policy.ArchivePolicy.from_dict(
                        metric['archive_policy'])),
                (storage.Measure(
                    m['timestamp'],
                    m['value']) for m in body))
        except storage.MetricDoesNotExist as e:
            pecan.abort(404, str(e))
        except storage.NoDeloreanAvailable as e:
            pecan.abort(400,
                        "The measure for %s is too old considering the "
                        "archive policy used by this metric. "
                        "It can only go back to %s."
                        % (e.bad_timestamp, e.first_timestamp))

    @pecan.expose('json')
    @pecan.expose('measures.j2')
    def get_measures(self, start=None, stop=None, aggregation='mean', **param):
        self.enforce_metric("get measures")
        if not (aggregation
                in archive_policy.ArchivePolicy.VALID_AGGREGATION_METHODS
                or aggregation in self.custom_agg):
            msg = '''Invalid aggregation value %(agg)s, must be one of %(std)s
                     or %(custom)s'''
            pecan.abort(400, msg % dict(
                agg=aggregation,
                std=archive_policy.ArchivePolicy.VALID_AGGREGATION_METHODS,
                custom=str(self.custom_agg.keys())))

        if start is not None:
            try:
                start = Timestamp(start)
            except Exception:
                pecan.abort(400, "Invalid value for start")

        if stop is not None:
            try:
                stop = Timestamp(stop)
            except Exception:
                pecan.abort(400, "Invalid value for stop")

        try:
            if aggregation in self.custom_agg:
                measures = self.custom_agg[aggregation].compute(
                    pecan.request.storage, self.metric_id, start, stop,
                    **param)
            else:
                measures = pecan.request.storage.get_measures(
                    # NOTE(jd) We don't set the archive policy in the object
                    # here because it's not used; but we could do it if needed
                    # by requesting the metric details from the indexer, for
                    # example in the enforce_metric() call above.
                    storage.Metric(name=self.metric_id, archive_policy=None),
                    start, stop, aggregation)
            # Replace timestamp keys by their string versions
            return [(timeutils.isotime(timestamp, subsecond=True), offset, v)
                    for timestamp, offset, v in measures]
        except storage.MetricDoesNotExist as e:
            pecan.abort(404, str(e))
        except aggregates.CustomAggFailure as e:
            pecan.abort(400, str(e))

    @pecan.expose()
    def delete(self):
        metric = self.enforce_metric("delete metric", details=True)[0]
        try:
            pecan.request.storage.delete_metric(
                storage.Metric(
                    self.metric_id,
                    archive_policy.ArchivePolicy.from_dict(
                        metric['archive_policy'])))
        except storage.MetricDoesNotExist as e:
            pecan.abort(404, str(e))
        pecan.request.indexer.delete_metric(self.metric_id)


def UUID(value):
    try:
        return uuid.UUID(value)
    except Exception as e:
        raise ValueError(e)


MetricSchemaDefinition = {
    "user_id": UUID,
    "project_id": UUID,
    voluptuous.Required('archive_policy_name'): six.text_type,
}


class MetricsController(rest.RestController):
    @staticmethod
    @pecan.expose()
    def _lookup(id, *remainder):
        # That's triggered when accessing /v1/metric/
        if id is "":
            pecan.abort(404)
        return MetricController(id), remainder

    Metric = voluptuous.Schema(MetricSchemaDefinition)

    @staticmethod
    def create_metric(created_by_user_id, created_by_project_id,
                      archive_policy_name,
                      user_id=None, project_id=None):
        enforce("create metric", {
            "created_by_user_id": created_by_user_id,
            "created_by_project_id": created_by_project_id,
            "user_id": user_id,
            "project_id": project_id,
            "archive_policy_name": archive_policy_name,
        })
        id = uuid.uuid4()
        policy = pecan.request.indexer.get_archive_policy(archive_policy_name)
        if policy is None:
            pecan.abort(400, "Unknown archive policy %s" % archive_policy_name)
        ap = archive_policy.ArchivePolicy.from_dict(policy)
        pecan.request.indexer.create_metric(
            id,
            created_by_user_id, created_by_project_id,
            archive_policy_name=policy['name'])
        pecan.request.storage.create_metric(storage.Metric(name=str(id),
                                                           archive_policy=ap))
        return id

    @vexpose(Metric, 'json')
    def post(self, body):
        user, project = get_user_and_project()
        id = self.create_metric(user, project, **body)
        set_resp_location_hdr("/v1/metric/" + str(id))
        pecan.response.status = 201
        return {"id": str(id),
                "archive_policy_name": str(body['archive_policy_name'])}

    @staticmethod
    @pecan.expose('json')
    def get_all(**kwargs):
        try:
            enforce("list all metric", {})
        except webob.exc.HTTPForbidden:
            enforce("list metric", {})
            user_id, project_id = get_user_and_project()
            provided_user_id = kwargs.get('user_id')
            provided_project_id = kwargs.get('project_id')
            if ((provided_user_id and user_id != provided_user_id)
               or (provided_project_id and project_id != provided_project_id)):
                pecan.abort(
                    403, "Insufficient privileges to filter by user/project")
        else:
            user_id = kwargs.get('user_id')
            project_id = kwargs.get('project_id')
        return pecan.request.indexer.list_metrics(
            user_id, project_id)


Metrics = voluptuous.Schema({
    six.text_type: voluptuous.Any(UUID,
                                  MetricsController.Metric),
})


class NamedMetricController(rest.RestController):
    def __init__(self, resource_id, resource_type):
        self.resource_id = resource_id
        self.resource_type = resource_type

    @pecan.expose()
    def _lookup(self, name, *remainder):
        try:
            uuid.UUID(self.resource_id)
        except ValueError:
            return (self._lookup_aggregated_metric(self.resource_id, name),
                    remainder)
        else:
            return self._lookup_metric(name), remainder

    def _lookup_metric(self, name):
        # TODO(jd) There might be an slight optimization to do by using a
        # dedicated driver method rather than get_resource, which might be
        # heavier.
        resource = pecan.request.indexer.get_resource(
            'generic', self.resource_id, with_metrics=True)
        if name in resource['metrics']:
            return MetricController(resource['metrics'][name])
        pecan.abort(404)

    def _lookup_aggregated_metric(self, query, name):
        attr_filter = self._get_filters_from_query(query)
        resources = pecan.request.indexer.list_resources(
            self.resource_type, attribute_filter=attr_filter)
        return AggregatedMetricController([r['metrics'][name]
                                           for r in resources])

    def _get_filters_from_query(self, query):
        # TODO(sileht): Implements more filters not just ∧
        parsed_query = {}
        for fragment in query.split(LOGICAL_AND):
            try:
                fragment = six.text_type(fragment)
            except ValueError:
                pecan.abort(400, "Invalid input: %s" % query)

            fragment = fragment.split('=', 1)
            if len(fragment) != 2:
                pecan.abort(400, "Invalid input: %s" % query)

            parsed_query[fragment[0]] = fragment[1]

        # TODO(sileht): for now to reduce the number of voluptuous
        # schema definition used we allows to filter only on Patchable
        # attributes, (voluptuous schema of Postable attributes
        # have 'required' set and we don't want to force to put all
        # the filters into the query)
        try:
            ctrl = getattr(ResourcesController, self.resource_type)
            schema = ctrl._resource_rest_class.ResourcePatch
        except AttributeError:
            pecan.abort(404)

        try:
            filters = schema(parsed_query)
        except voluptuous.Error as e:
            pecan.abort(400, "Invalid input: %s" % e)

        return {"and":
                [{"=": {k: v}}
                 for k, v in six.iteritems(filters)]}

    @vexpose(Metrics)
    def post(self, body):
        resource = pecan.request.indexer.get_resource(
            self.resource_type, self.resource_id)
        enforce("update resource", resource)
        user, project = get_user_and_project()
        metrics = convert_metric_list(body, user, project)
        try:
            pecan.request.indexer.update_resource(
                self.resource_type, self.resource_id, metrics=metrics,
                append_metrics=True)
        except (indexer.NoSuchMetric, ValueError) as e:
            pecan.abort(400, e)
        except indexer.NamedMetricAlreadyExists as e:
            pecan.abort(409, e)
        except indexer.NoSuchResource as e:
            pecan.abort(404, e)


Metrics = voluptuous.Schema({
    six.text_type: voluptuous.Any(UUID,
                                  MetricsController.Metric),
})


def ResourceSchema(schema):
    base_schema = {
        voluptuous.Required("id"): UUID,
        'started_at': Timestamp,
        'ended_at': Timestamp,
        'user_id': voluptuous.Any(None, UUID),
        'project_id': voluptuous.Any(None, UUID),
        'metrics': Metrics,
    }
    base_schema.update(schema)
    return voluptuous.Schema(base_schema)


def ResourcePatchSchema(schema):
    base_schema = {
        'metrics': Metrics,
        'ended_at': Timestamp,
    }
    base_schema.update(schema)
    return voluptuous.Schema(base_schema)


class GenericResourceController(rest.RestController):
    _resource_type = 'generic'

    ResourcePatch = ResourcePatchSchema({})

    def __init__(self, id):
        self.id = id
        self.metric = NamedMetricController(id, self._resource_type)

    @pecan.expose('json')
    @pecan.expose('resources.j2')
    def get(self):
        resource = pecan.request.indexer.get_resource(
            self._resource_type, self.id, with_metrics=True)
        if resource:
            enforce("get resource", resource)
            return resource
        pecan.abort(404)

    @pecan.expose()
    def patch(self):
        resource = pecan.request.indexer.get_resource(
            self._resource_type, self.id)
        if not resource:
            pecan.abort(404)
        enforce("update resource", resource)
        # NOTE(jd) Can't use vexpose because it does not take into account
        # inheritance
        body = deserialize(self.ResourcePatch)
        if len(body) == 0:
            return

        try:
            if 'metrics' in body:
                user, project = get_user_and_project()
                body['metrics'] = convert_metric_list(
                    body['metrics'], user, project)
            pecan.request.indexer.update_resource(
                self._resource_type,
                self.id, **body)
        except (indexer.NoSuchMetric, ValueError) as e:
            pecan.abort(400, e)
        except indexer.NoSuchResource as e:
            pecan.abort(404, e)

    @staticmethod
    def _delete_metrics(metrics):
        for metric in metrics:
            enforce("delete metric", metric)
        for metric in metrics:
            try:
                pecan.request.storage.delete_metric(
                    storage.Metric(str(metric['id']),
                                   archive_policy.ArchivePolicy.from_dict(
                                       metric['archive_policy'])))
            except Exception:
                LOG.error(
                    "Unable to delete metric `%s' from storage, "
                    "you will need to delete it manually" % metric,
                    exc_info=True)

    @pecan.expose()
    def delete(self):
        resource = pecan.request.indexer.get_resource(
            self._resource_type, self.id)
        if not resource:
            pecan.abort(404, indexer.NoSuchResource(self.id))
        enforce("delete resource", resource)
        try:
            pecan.request.indexer.delete_resource(
                self.id,
                delete_metrics=self._delete_metrics)
        except indexer.NoSuchResource as e:
            pecan.abort(404, str(e))


class SwiftAccountResourceController(GenericResourceController):
    _resource_type = 'swift_account'


class InstanceResourceController(GenericResourceController):
    _resource_type = 'instance'

    ResourcePatch = ResourcePatchSchema({
        "flavor_id": int,
        "image_ref": six.text_type,
        "host": six.text_type,
        "display_name": six.text_type,
        "server_group": six.text_type,
    })


class VolumeResourceController(GenericResourceController):
    _resource_type = 'volume'

    ResourcePatch = ResourcePatchSchema({
        "display_name": six.text_type,
    })


class GenericResourcesController(rest.RestController):
    _resource_type = 'generic'
    _resource_rest_class = GenericResourceController

    Resource = ResourceSchema({})

    @pecan.expose()
    def _lookup(self, id, *remainder):
        return self._resource_rest_class(id), remainder

    @pecan.expose('json')
    def post(self):
        # NOTE(jd) Can't use vexpose because it does not take into account
        # inheritance
        body = deserialize(self.Resource)
        target = {
            "resource_type": self._resource_type,
        }
        target.update(body)
        enforce("create resource", target)
        user, project = get_user_and_project()
        body['metrics'] = convert_metric_list(
            body.get('metrics', {}), user, project)
        rid = body['id']
        del body['id']
        try:
            resource = pecan.request.indexer.create_resource(
                self._resource_type, rid, user, project,
                **body)
        except (ValueError, indexer.NoSuchMetric) as e:
            pecan.abort(400, e)
        except indexer.ResourceAlreadyExists as e:
            pecan.abort(409, e)
        set_resp_location_hdr("/v1/resource/"
                              + self._resource_type + "/"
                              + six.text_type(resource['id']))
        pecan.response.status = 201
        return resource

    @pecan.expose('json')
    def get_all(self, **kwargs):
        details = get_details(kwargs)

        try:
            enforce("list all resource", {
                "resource_type": self._resource_type,
            })
        except webob.exc.HTTPForbidden:
            enforce("list resource", {
                "resource_type": self._resource_type,
            })
            user, project = get_user_and_project()
            attr_filter = {"and": [{"=": {"created_by_user_id": user}},
                                   {"=": {"created_by_project_id": project}}]}
        else:
            attr_filter = {}

        try:
            return pecan.request.indexer.list_resources(
                self._resource_type,
                attribute_filter=attr_filter,
                details=details)
        except indexer.ResourceAttributeError as e:
            pecan.abort(400, e)


class SwiftAccountsResourcesController(GenericResourcesController):
    _resource_type = 'swift_account'
    _resource_rest_class = SwiftAccountResourceController


class InstancesResourcesController(GenericResourcesController):
    _resource_type = 'instance'
    _resource_rest_class = InstanceResourceController

    Resource = ResourceSchema({
        voluptuous.Required("flavor_id"): int,
        voluptuous.Required("image_ref"): six.text_type,
        voluptuous.Required("host"): six.text_type,
        voluptuous.Required("display_name"): six.text_type,
        "server_group": six.text_type,
    })


class VolumesResourcesController(GenericResourcesController):
    _resource_type = 'volume'
    _resource_rest_class = VolumeResourceController

    Resource = ResourceSchema({
        voluptuous.Required("display_name"): six.text_type,
    })


class ResourcesController(rest.RestController):
    generic = GenericResourcesController()
    instance = InstancesResourcesController()
    swift_account = SwiftAccountsResourcesController()
    volume = VolumesResourcesController()


def _SearchSchema(v):
    """Helper method to indirect the recursivity of the search schema"""
    return SearchResourceTypeController.SearchSchema(v)


class SearchResourceTypeController(rest.RestController):
    def __init__(self, resource_type):
        self._resource_type = resource_type

    SearchSchema = voluptuous.Schema(
        voluptuous.All(
            voluptuous.Length(min=1, max=1),
            {
                voluptuous.Any("=", "<=", ">=", "!=", "in", "like"):
                voluptuous.All(voluptuous.Length(min=1, max=1), dict),
                voluptuous.Any("and", "or", "not"): [_SearchSchema],
            }
        )
    )

    @pecan.expose('json')
    def post(self, **kwargs):
        if pecan.request.body:
            attr_filter = deserialize(self.SearchSchema)
        else:
            attr_filter = {}

        details = get_details(kwargs)

        try:
            enforce("search all resource", {
                "resource_type": self._resource_type,
            })
        except webob.exc.HTTPForbidden:
            enforce("search resource", {
                "resource_type": self._resource_type,
            })
            user, project = get_user_and_project()
            attr_filter = {"and": [{"=": {"created_by_user_id": user}},
                                   {"=": {"created_by_project_id": project}},
                                   attr_filter]}

        try:
            return pecan.request.indexer.list_resources(
                self._resource_type,
                attribute_filter=attr_filter,
                details=details)
        except indexer.ResourceAttributeError as e:
            pecan.abort(400, e)


class SearchResourceController(rest.RestController):
    @pecan.expose()
    def _lookup(self, resource_type, *remainder):
        # TODO(jd) Check that resource_type is valid
        return SearchResourceTypeController(resource_type), remainder


class SearchController(rest.RestController):
    resource = SearchResourceController()


class V1Controller(rest.RestController):
    search = SearchController()

    archive_policy = ArchivePoliciesController()
    metric = MetricsController()
    resource = ResourcesController()

    _custom_actions = {
        'capabilities': ['GET'],
        'metric_aggregation': ['GET'],
    }

    @staticmethod
    @pecan.expose('json')
    def get_capabilities():
        aggregation_methods = set(
            archive_policy.ArchivePolicy.VALID_AGGREGATION_METHODS)
        aggregation_methods.update(
            ext.name for ext in extension.ExtensionManager(
                namespace='gnocchi.aggregates'))
        return dict(aggregation_methods=aggregation_methods)

    @pecan.expose('json')
    def get_metric_aggregation(self, metric=None, start=None,
                               stop=None, aggregation='mean',
                               needed_overlap=100.0):
        if isinstance(metric, list):
            metrics = metric
        elif metric:
            metrics = [metric]
        else:
            metrics = []
        return AggregatedMetricController.get_cross_metric_measures(
            metrics, start, stop, aggregation, needed_overlap)


class RootController(object):
    v1 = V1Controller()

    @staticmethod
    @pecan.expose(content_type="text/plain")
    def index():
        return "Nom nom nom."
