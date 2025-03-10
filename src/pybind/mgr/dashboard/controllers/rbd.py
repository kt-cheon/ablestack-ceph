# -*- coding: utf-8 -*-
# pylint: disable=unused-argument
# pylint: disable=too-many-statements,too-many-branches

import logging
import math
from datetime import datetime
from functools import partial

import rbd

from .. import mgr
from ..exceptions import DashboardException
from ..security import Scope
from ..services.ceph_service import CephService
from ..services.exception import handle_rados_error, handle_rbd_error, serialize_dashboard_exception
from ..services.rbd import MIRROR_IMAGE_MODE, RbdConfiguration, \
    RbdMirroringService, RbdService, RbdSnapshotService, format_bitmask, \
    format_features, get_image_spec, parse_image_spec, rbd_call, \
    rbd_image_call
from ..tools import ViewCache, str_to_bool
from . import APIDoc, APIRouter, BaseController, CreatePermission, \
    DeletePermission, Endpoint, EndpointDoc, ReadPermission, RESTController, \
    Task, UIRouter, UpdatePermission, allow_empty_body

logger = logging.getLogger(__name__)

RBD_SCHEMA = ([{
    "status": (int, 'Status of the image'),
    "value": ([str], ''),
    "pool_name": (str, 'pool name')
}])

RBD_TRASH_SCHEMA = [{
    "status": (int, ''),
    "value": ([str], ''),
    "pool_name": (str, 'pool name')
}]


# pylint: disable=not-callable
def RbdTask(name, metadata, wait_for):  # noqa: N802
    def composed_decorator(func):
        func = handle_rados_error('pool')(func)
        func = handle_rbd_error()(func)
        return Task("rbd/{}".format(name), metadata, wait_for,
                    partial(serialize_dashboard_exception, include_http_status=True))(func)
    return composed_decorator


def _sort_features(features, enable=True):
    """
    Sorts image features according to feature dependencies:

    object-map depends on exclusive-lock
    journaling depends on exclusive-lock
    fast-diff depends on object-map
    """
    ORDER = ['exclusive-lock', 'journaling', 'object-map', 'fast-diff']  # noqa: N806

    def key_func(feat):
        try:
            return ORDER.index(feat)
        except ValueError:
            return id(feat)

    features.sort(key=key_func, reverse=not enable)


@APIRouter('/block/image', Scope.RBD_IMAGE)
@APIDoc("RBD Management API", "Rbd")
class Rbd(RESTController):

    # set of image features that can be enable on existing images
    ALLOW_ENABLE_FEATURES = {"exclusive-lock", "object-map", "fast-diff", "journaling"}

    # set of image features that can be disabled on existing images
    ALLOW_DISABLE_FEATURES = {"exclusive-lock", "object-map", "fast-diff", "deep-flatten",
                              "journaling"}

    def _rbd_list(self, pool_name=None):
        if pool_name:
            pools = [pool_name]
        else:
            pools = [p['pool_name'] for p in CephService.get_pool_list('rbd')]

        result = []
        for pool in pools:
            # pylint: disable=unbalanced-tuple-unpacking
            status, value = RbdService.rbd_pool_list(pool)
            for i, image in enumerate(value):
                value[i]['configuration'] = RbdConfiguration(
                    pool, image['namespace'], image['name']).list()
            result.append({'status': status, 'value': value, 'pool_name': pool})
        return result

    @handle_rbd_error()
    @handle_rados_error('pool')
    @EndpointDoc("Display Rbd Images",
                 parameters={
                     'pool_name': (str, 'Pool Name'),
                 },
                 responses={200: RBD_SCHEMA})
    def list(self, pool_name=None):
        return self._rbd_list(pool_name)

    @handle_rbd_error()
    @handle_rados_error('pool')
    def get(self, image_spec):
        return RbdService.get_image(image_spec)

    @RbdTask('create',
             {'pool_name': '{pool_name}', 'namespace': '{namespace}', 'image_name': '{name}'}, 2.0)
    def create(self, name, pool_name, size, namespace=None, schedule_interval='',
               obj_size=None, features=None, stripe_unit=None, stripe_count=None,
               data_pool=None, configuration=None, mirror_mode=None):

        size = int(size)

        def _create(ioctx):
            rbd_inst = rbd.RBD()

            # Set order
            l_order = None
            if obj_size and obj_size > 0:
                l_order = int(round(math.log(float(obj_size), 2)))

            # Set features
            feature_bitmask = format_features(features)

            rbd_inst.create(ioctx, name, size, order=l_order, old_format=False,
                            features=feature_bitmask, stripe_unit=stripe_unit,
                            stripe_count=stripe_count, data_pool=data_pool)
            RbdConfiguration(pool_ioctx=ioctx, namespace=namespace,
                             image_name=name).set_configuration(configuration)

        rbd_call(pool_name, namespace, _create)
        if mirror_mode:
            RbdMirroringService.enable_image(name, pool_name, namespace,
                                             MIRROR_IMAGE_MODE[mirror_mode])

        if schedule_interval:
            image_spec = get_image_spec(pool_name, namespace, name)
            RbdMirroringService.snapshot_schedule_add(image_spec, schedule_interval)

    @RbdTask('delete', ['{image_spec}'], 2.0)
    def delete(self, image_spec):
        pool_name, namespace, image_name = parse_image_spec(image_spec)

        image = RbdService.get_image(image_spec)
        snapshots = image['snapshots']
        for snap in snapshots:
            RbdSnapshotService.remove_snapshot(image_spec, snap['name'], snap['is_protected'])

        rbd_inst = rbd.RBD()
        return rbd_call(pool_name, namespace, rbd_inst.remove, image_name)

    @RbdTask('edit', ['{image_spec}', '{name}'], 4.0)
    def set(self, image_spec, name=None, size=None, features=None,
            configuration=None, enable_mirror=None, primary=None,
            resync=False, mirror_mode=None, schedule_interval='',
            remove_scheduling=False):

        pool_name, namespace, image_name = parse_image_spec(image_spec)

        def _edit(ioctx, image):
            rbd_inst = rbd.RBD()
            # check rename image
            if name and name != image_name:
                rbd_inst.rename(ioctx, image_name, name)

            # check resize
            if size and size != image.size():
                image.resize(size)

            # check enable/disable features
            if features is not None:
                curr_features = format_bitmask(image.features())
                # check disabled features
                _sort_features(curr_features, enable=False)
                for feature in curr_features:
                    if (feature not in features
                       and feature in self.ALLOW_DISABLE_FEATURES
                       and feature in format_bitmask(image.features())):
                        f_bitmask = format_features([feature])
                        image.update_features(f_bitmask, False)
                # check enabled features
                _sort_features(features)
                for feature in features:
                    if (feature not in curr_features
                       and feature in self.ALLOW_ENABLE_FEATURES
                       and feature not in format_bitmask(image.features())):
                        f_bitmask = format_features([feature])
                        image.update_features(f_bitmask, True)

            RbdConfiguration(pool_ioctx=ioctx, image_name=image_name).set_configuration(
                configuration)

            mirror_image_info = image.mirror_image_get_info()
            if enable_mirror and mirror_image_info['state'] == rbd.RBD_MIRROR_IMAGE_DISABLED:
                RbdMirroringService.enable_image(
                    image_name, pool_name, namespace,
                    MIRROR_IMAGE_MODE[mirror_mode])
            elif (enable_mirror is False
                  and mirror_image_info['state'] == rbd.RBD_MIRROR_IMAGE_ENABLED):
                RbdMirroringService.disable_image(
                    image_name, pool_name, namespace)

            if primary and not mirror_image_info['primary']:
                RbdMirroringService.promote_image(
                    image_name, pool_name, namespace)
            elif primary is False and mirror_image_info['primary']:
                RbdMirroringService.demote_image(
                    image_name, pool_name, namespace)

            if resync:
                RbdMirroringService.resync_image(image_name, pool_name, namespace)

            if schedule_interval:
                RbdMirroringService.snapshot_schedule_add(image_spec, schedule_interval)

            if remove_scheduling:
                RbdMirroringService.snapshot_schedule_remove(image_spec)

        return rbd_image_call(pool_name, namespace, image_name, _edit)

    @RbdTask('copy',
             {'src_image_spec': '{image_spec}',
              'dest_pool_name': '{dest_pool_name}',
              'dest_namespace': '{dest_namespace}',
              'dest_image_name': '{dest_image_name}'}, 2.0)
    @RESTController.Resource('POST')
    @allow_empty_body
    def copy(self, image_spec, dest_pool_name, dest_namespace, dest_image_name,
             snapshot_name=None, obj_size=None, features=None,
             stripe_unit=None, stripe_count=None, data_pool=None, configuration=None):
        pool_name, namespace, image_name = parse_image_spec(image_spec)

        def _src_copy(s_ioctx, s_img):
            def _copy(d_ioctx):
                # Set order
                l_order = None
                if obj_size and obj_size > 0:
                    l_order = int(round(math.log(float(obj_size), 2)))

                # Set features
                feature_bitmask = format_features(features)

                if snapshot_name:
                    s_img.set_snap(snapshot_name)

                s_img.copy(d_ioctx, dest_image_name, feature_bitmask, l_order,
                           stripe_unit, stripe_count, data_pool)
                RbdConfiguration(pool_ioctx=d_ioctx, image_name=dest_image_name).set_configuration(
                    configuration)

            return rbd_call(dest_pool_name, dest_namespace, _copy)

        return rbd_image_call(pool_name, namespace, image_name, _src_copy)

    @RbdTask('flatten', ['{image_spec}'], 2.0)
    @RESTController.Resource('POST')
    @UpdatePermission
    @allow_empty_body
    def flatten(self, image_spec):

        def _flatten(ioctx, image):
            image.flatten()

        pool_name, namespace, image_name = parse_image_spec(image_spec)
        return rbd_image_call(pool_name, namespace, image_name, _flatten)

    @RESTController.Collection('GET')
    def default_features(self):
        rbd_default_features = mgr.get('config')['rbd_default_features']
        return format_bitmask(int(rbd_default_features))

    @RESTController.Collection('GET')
    def clone_format_version(self):
        """Return the RBD clone format version.
        """
        rbd_default_clone_format = mgr.get('config')['rbd_default_clone_format']
        if rbd_default_clone_format != 'auto':
            return int(rbd_default_clone_format)
        osd_map = mgr.get_osdmap().dump()
        min_compat_client = osd_map.get('min_compat_client', '')
        require_min_compat_client = osd_map.get('require_min_compat_client', '')
        if max(min_compat_client, require_min_compat_client) < 'mimic':
            return 1

        return 2

    @RbdTask('trash/move', ['{image_spec}'], 2.0)
    @RESTController.Resource('POST')
    @allow_empty_body
    def move_trash(self, image_spec, delay=0):
        """Move an image to the trash.
        Images, even ones actively in-use by clones,
        can be moved to the trash and deleted at a later time.
        """
        pool_name, namespace, image_name = parse_image_spec(image_spec)
        rbd_inst = rbd.RBD()
        return rbd_call(pool_name, namespace, rbd_inst.trash_move, image_name, delay)


@UIRouter('/block/rbd')
class RbdStatus(BaseController):
    @EndpointDoc("Display RBD Image feature status")
    @Endpoint()
    @ReadPermission
    def status(self):
        status = {'available': True, 'message': None}
        if not CephService.get_pool_list('rbd'):
            status['available'] = False
            status['message'] = 'No RBD pools in the cluster. Please create a pool '\
                                'with the "rbd" application label.'  # type: ignore
        return status


@APIRouter('/block/image/{image_spec}/snap', Scope.RBD_IMAGE)
@APIDoc("RBD Snapshot Management API", "RbdSnapshot")
class RbdSnapshot(RESTController):

    RESOURCE_ID = "snapshot_name"

    @RbdTask('snap/create',
             ['{image_spec}', '{snapshot_name}'], 2.0)
    def create(self, image_spec, snapshot_name):
        pool_name, namespace, image_name = parse_image_spec(image_spec)

        def _create_snapshot(ioctx, img, snapshot_name):
            mirror_info = img.mirror_image_get_info()
            mirror_mode = img.mirror_image_get_mode()
            if (mirror_info['state'] == rbd.RBD_MIRROR_IMAGE_ENABLED
                    and mirror_mode == rbd.RBD_MIRROR_IMAGE_MODE_SNAPSHOT):
                img.mirror_image_create_snapshot()
            else:
                img.create_snap(snapshot_name)

        return rbd_image_call(pool_name, namespace, image_name, _create_snapshot,
                              snapshot_name)

    @RbdTask('snap/delete',
             ['{image_spec}', '{snapshot_name}'], 2.0)
    def delete(self, image_spec, snapshot_name):
        return RbdSnapshotService.remove_snapshot(image_spec, snapshot_name)

    @RbdTask('snap/edit',
             ['{image_spec}', '{snapshot_name}'], 4.0)
    def set(self, image_spec, snapshot_name, new_snap_name=None,
            is_protected=None):
        def _edit(ioctx, img, snapshot_name):
            if new_snap_name and new_snap_name != snapshot_name:
                img.rename_snap(snapshot_name, new_snap_name)
                snapshot_name = new_snap_name
            if is_protected is not None and \
                    is_protected != img.is_protected_snap(snapshot_name):
                if is_protected:
                    img.protect_snap(snapshot_name)
                else:
                    img.unprotect_snap(snapshot_name)

        pool_name, namespace, image_name = parse_image_spec(image_spec)
        return rbd_image_call(pool_name, namespace, image_name, _edit, snapshot_name)

    @RbdTask('snap/rollback',
             ['{image_spec}', '{snapshot_name}'], 5.0)
    @RESTController.Resource('POST')
    @UpdatePermission
    @allow_empty_body
    def rollback(self, image_spec, snapshot_name):
        def _rollback(ioctx, img, snapshot_name):
            img.rollback_to_snap(snapshot_name)

        pool_name, namespace, image_name = parse_image_spec(image_spec)
        return rbd_image_call(pool_name, namespace, image_name, _rollback, snapshot_name)

    @RbdTask('clone',
             {'parent_image_spec': '{image_spec}',
              'child_pool_name': '{child_pool_name}',
              'child_namespace': '{child_namespace}',
              'child_image_name': '{child_image_name}'}, 2.0)
    @RESTController.Resource('POST')
    @allow_empty_body
    def clone(self, image_spec, snapshot_name, child_pool_name,
              child_image_name, child_namespace=None, obj_size=None, features=None,
              stripe_unit=None, stripe_count=None, data_pool=None, configuration=None):
        """
        Clones a snapshot to an image
        """

        pool_name, namespace, image_name = parse_image_spec(image_spec)

        def _parent_clone(p_ioctx):
            def _clone(ioctx):
                # Set order
                l_order = None
                if obj_size and obj_size > 0:
                    l_order = int(round(math.log(float(obj_size), 2)))

                # Set features
                feature_bitmask = format_features(features)

                rbd_inst = rbd.RBD()
                rbd_inst.clone(p_ioctx, image_name, snapshot_name, ioctx,
                               child_image_name, feature_bitmask, l_order,
                               stripe_unit, stripe_count, data_pool)

                RbdConfiguration(pool_ioctx=ioctx, image_name=child_image_name).set_configuration(
                    configuration)

            return rbd_call(child_pool_name, child_namespace, _clone)

        rbd_call(pool_name, namespace, _parent_clone)


@APIRouter('/block/image/trash', Scope.RBD_IMAGE)
@APIDoc("RBD Trash Management API", "RbdTrash")
class RbdTrash(RESTController):
    RESOURCE_ID = "image_id_spec"

    def __init__(self):
        super().__init__()
        self.rbd_inst = rbd.RBD()

    @ViewCache()
    def _trash_pool_list(self, pool_name):
        with mgr.rados.open_ioctx(pool_name) as ioctx:
            result = []
            namespaces = self.rbd_inst.namespace_list(ioctx)
            # images without namespace
            namespaces.append('')
            for namespace in namespaces:
                ioctx.set_namespace(namespace)
                images = self.rbd_inst.trash_list(ioctx)
                for trash in images:
                    trash['pool_name'] = pool_name
                    trash['namespace'] = namespace
                    trash['deletion_time'] = "{}Z".format(trash['deletion_time'].isoformat())
                    trash['deferment_end_time'] = "{}Z".format(
                        trash['deferment_end_time'].isoformat())
                    result.append(trash)
            return result

    def _trash_list(self, pool_name=None):
        if pool_name:
            pools = [pool_name]
        else:
            pools = [p['pool_name'] for p in CephService.get_pool_list('rbd')]

        result = []
        for pool in pools:
            # pylint: disable=unbalanced-tuple-unpacking
            status, value = self._trash_pool_list(pool)
            result.append({'status': status, 'value': value, 'pool_name': pool})
        return result

    @handle_rbd_error()
    @handle_rados_error('pool')
    @EndpointDoc("Get RBD Trash Details by pool name",
                 parameters={
                     'pool_name': (str, 'Name of the pool'),
                 },
                 responses={200: RBD_TRASH_SCHEMA})
    def list(self, pool_name=None):
        """List all entries from trash."""
        return self._trash_list(pool_name)

    @handle_rbd_error()
    @handle_rados_error('pool')
    @RbdTask('trash/purge', ['{pool_name}'], 2.0)
    @RESTController.Collection('POST', query_params=['pool_name'])
    @DeletePermission
    @allow_empty_body
    def purge(self, pool_name=None):
        """Remove all expired images from trash."""
        now = "{}Z".format(datetime.utcnow().isoformat())
        pools = self._trash_list(pool_name)

        for pool in pools:
            for image in pool['value']:
                if image['deferment_end_time'] < now:
                    logger.info('Removing trash image %s (pool=%s, namespace=%s, name=%s)',
                                image['id'], pool['pool_name'], image['namespace'], image['name'])
                    rbd_call(pool['pool_name'], image['namespace'],
                             self.rbd_inst.trash_remove, image['id'], 0)

    @RbdTask('trash/restore', ['{image_id_spec}', '{new_image_name}'], 2.0)
    @RESTController.Resource('POST')
    @CreatePermission
    @allow_empty_body
    def restore(self, image_id_spec, new_image_name):
        """Restore an image from trash."""
        pool_name, namespace, image_id = parse_image_spec(image_id_spec)
        return rbd_call(pool_name, namespace, self.rbd_inst.trash_restore, image_id,
                        new_image_name)

    @RbdTask('trash/remove', ['{image_id_spec}'], 2.0)
    def delete(self, image_id_spec, force=False):
        """Delete an image from trash.
        If image deferment time has not expired you can not removed it unless use force.
        But an actively in-use by clones or has snapshots can not be removed.
        """
        pool_name, namespace, image_id = parse_image_spec(image_id_spec)
        return rbd_call(pool_name, namespace, self.rbd_inst.trash_remove, image_id,
                        int(str_to_bool(force)))


@APIRouter('/block/pool/{pool_name}/namespace', Scope.RBD_IMAGE)
@APIDoc("RBD Namespace Management API", "RbdNamespace")
class RbdNamespace(RESTController):

    def __init__(self):
        super().__init__()
        self.rbd_inst = rbd.RBD()

    def create(self, pool_name, namespace):
        with mgr.rados.open_ioctx(pool_name) as ioctx:
            namespaces = self.rbd_inst.namespace_list(ioctx)
            if namespace in namespaces:
                raise DashboardException(
                    msg='Namespace already exists',
                    code='namespace_already_exists',
                    component='rbd')
            return self.rbd_inst.namespace_create(ioctx, namespace)

    def delete(self, pool_name, namespace):
        with mgr.rados.open_ioctx(pool_name) as ioctx:
            # pylint: disable=unbalanced-tuple-unpacking
            _, images = RbdService.rbd_pool_list(pool_name, namespace)
            if images:
                raise DashboardException(
                    msg='Namespace contains images which must be deleted first',
                    code='namespace_contains_images',
                    component='rbd')
            return self.rbd_inst.namespace_remove(ioctx, namespace)

    def list(self, pool_name):
        with mgr.rados.open_ioctx(pool_name) as ioctx:
            result = []
            namespaces = self.rbd_inst.namespace_list(ioctx)
            for namespace in namespaces:
                # pylint: disable=unbalanced-tuple-unpacking
                _, images = RbdService.rbd_pool_list(pool_name, namespace)
                result.append({
                    'namespace': namespace,
                    'num_images': len(images) if images else 0
                })
            return result
