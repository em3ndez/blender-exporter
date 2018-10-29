"""Export animation into Godot scene tree"""
import collections
import re
import math
import copy
import logging
from functools import partial
import bpy
import bpy_extras.anim_utils
import mathutils
from ..structures import (NodeTemplate, NodePath, fix_directional_transform,
                          InternalResource, Array, Map, fix_matrix,
                          fix_bone_attachment_transform)

NEAREST_INTERPOLATION = 0
LINEAR_INTERPOLATION = 1


# attribute converted as a bool, no interpolation
CONVERT_AS_BOOL = 0
# attribute converted as a float
CONVERT_AS_FLOAT = 1
# attribute is a vec or mat, mapping to several fcurves in animation
CONVERT_AS_MULTI_VALUE = 2
# a quad tuple contains information to convert an attribute
# or a fcurve of blender to godot things
AttributeConvertInfo = collections.namedtuple(
    'AttributeConvertInfo',
    ['bl_name', 'gd_name', 'converter_function', 'attribute_type']
)

# a suffix append to action need baking to avoid name collision
# with baked action's name
BAKING_SUFFIX = '--being-baking'


class Track:
    """Animation track, has track type, track path, interpolation
    method, a list of frames and a list of frame values.

    Note that element in value_list is not strictly typed, for example,
    a transform track would have value with type mathutils.Matrix(),
    while some track would just have a float value"""
    def __init__(self, track_type, track_path,
                 frames=(), values=()):
        self.type = track_type
        self.path = track_path
        # default to linear
        self.interp = LINEAR_INTERPOLATION
        self.frames = list()
        self.values = list()

        for frame in frames:
            self.frames.append(frame)
        for value in values:
            self.values.append(value)

    def add_frame_data(self, frame, value):
        """Add add frame to track"""
        self.frames.append(frame)
        self.values.append(value)

    def frame_end(self):
        """The frame number of last frame"""
        if not self.frames:
            return 0
        return self.frames[-1]

    def frame_begin(self):
        """The frame number of first frame"""
        if not self.frames:
            return 0
        return self.frames[0]


class AnimationResource(InternalResource):
    """Internal resource with type Animation"""
    def __init__(self, name):
        super().__init__('Animation', name)
        self['step'] = 0.1
        self['length'] = 0
        self.track_count = 0

    def add_track(self, track):
        """add a track to animation resource"""
        track_length = track.frame_end() / bpy.context.scene.render.fps
        if track_length > self['length']:
            self['length'] = track_length

        track_id_str = 'tracks/{}'.format(self.track_count)
        self.track_count += 1

        self[track_id_str + '/type'] = '"{}"'.format(track.type)
        self[track_id_str + '/path'] = track.path
        self[track_id_str + '/interp'] = track.interp
        if track.type == 'transform':
            self[track_id_str + '/keys'] = transform_frames_to_keys(
                track.frames, track.values, track.interp
            )
        elif track.type == 'value':
            self[track_id_str + '/keys'] = value_frames_to_keys(
                track.frames, track.values, track.interp
            )

    def add_track_via_attr_mapping(self, fcurves, conv_quad_tuple_list,
                                   base_node_path):
        """Accepts some attribute mapping relation between blender and godot,
        and further call `add_simple_value_track` export tracks.

        `conv_quad_tuple_list` is a list of quad tuple which compose of
        of(bl_attr_name, gd_attr_name, converter_lambda, attr_type)"""
        for item in conv_quad_tuple_list:
            bl_attr, gd_attr, converter, val_type = item
            # vector vallue animation need special treatment
            if val_type in (CONVERT_AS_FLOAT, CONVERT_AS_BOOL):
                if val_type == CONVERT_AS_FLOAT:
                    track_builder = build_linear_interp_value_track
                else:
                    track_builder = build_const_interp_value_track
                self.add_simple_value_track(
                    fcurves,
                    bl_attr,
                    partial(
                        track_builder,
                        base_node_path.new_copy(gd_attr),
                        converter
                    )
                )

    def add_simple_value_track(self, fcurves, fcurve_data_path,
                               fcurve_to_track_func):
        """Add a simple value track into AnimationResource, simple value
        track means it have a one-one mapping to fcurve.

        Note that the fcurve_to_track_func is a partial of
        function like build_linear_interp_value_track and
        build_const_interp_value_track which create a track
        from fcurve"""
        fcurve = fcurves.find(fcurve_data_path)
        if fcurve is not None:
            new_track = fcurve_to_track_func(fcurve)
            self.add_track(new_track)


class AnimationPlayer(NodeTemplate):
    """Godot scene node with type AnimationPlayer"""
    def __init__(self, name, parent):
        super().__init__(name, "AnimationPlayer", parent)
        # use parent node as the animation root node
        self['root_node'] = NodePath(self.get_path(), parent.get_path())
        # blender actions not in nla_tracks are treated as default
        self.default_animation = None

    def add_default_animation_resource(self, escn_file, action):
        """Default animation resource may hold animation from children
        objects, parameter action is used as hash key of resource"""
        self.default_animation = self.create_animation_resource(
            escn_file, action
        )

    def create_animation_resource(self, escn_file, action):
        """Create a new animation resource and add it into escn file"""
        resource_name = action.name
        new_anim_resource = AnimationResource(resource_name)

        # add animation resource without checking hash,
        # blender action is in world space, while godot animation
        # is in local space (parent space),  so identical actions
        # are not necessarily generates identical godot animations
        resource_id = escn_file.force_add_internal_resource(new_anim_resource)

        self['anims/{}'.format(resource_name)] = (
            "SubResource({})".format(resource_id))

        return new_anim_resource


def value_frames_to_keys(frame_list, value_list, interp):
    """Serialize a value list to a track keys object"""
    time_array = Array(prefix='PoolRealArray(', suffix=')')
    transition_array = Array(prefix='PoolRealArray(', suffix=')')
    value_array = Array(prefix='[', suffix=']')
    for index, frame in enumerate(frame_list):
        if (interp == LINEAR_INTERPOLATION and index > 0 and
                value_list[index] == value_list[index - 1]):
            continue

        time = frame / bpy.context.scene.render.fps
        time_array.append(time)
        transition_array.append(1)
        value_array.append(value_list[index])

    keys_map = Map()
    keys_map["times"] = time_array.to_string()
    keys_map["transitions"] = transition_array.to_string()
    keys_map["update"] = 0
    keys_map["values"] = value_array.to_string()

    return keys_map


def transform_frames_to_keys(frame_list, value_list, interp):
    """Convert a list of transform matrix to the keyframes
    of an animation track"""
    array = Array(prefix='[', suffix=']')
    for index, frame in enumerate(frame_list):
        if (interp == LINEAR_INTERPOLATION and index > 0 and
                value_list[index] == value_list[index - 1]):
            # do not export same keyframe
            continue

        array.append(frame / bpy.context.scene.render.fps)

        # transition default 1.0
        array.append(1.0)

        # convert from z-up to y-up
        mat = value_list[index]
        transform_mat = fix_matrix(mat)
        location = transform_mat.to_translation()
        quaternion = transform_mat.to_quaternion()
        scale = transform_mat.to_scale()

        array.append(location.x)
        array.append(location.y)
        array.append(location.z)
        array.append(quaternion.x)
        array.append(quaternion.y)
        array.append(quaternion.z)
        array.append(quaternion.w)
        array.append(scale.x)
        array.append(scale.y)
        array.append(scale.z)

    return array


def get_animation_player(escn_file, export_settings, godot_node):
    """Get a AnimationPlayer node, its return value depends
    on animation exporting settings"""
    animation_player = None
    # the parent of AnimationPlayer
    animation_base = None

    if export_settings['animation_modes'] == 'ACTIONS':
        animation_base = godot_node
    elif export_settings['animation_modes'] == 'SCENE_ANIMATION':
        node_ptr = godot_node
        while node_ptr.parent is not None:
            node_ptr = node_ptr.parent
        scene_root = node_ptr
        animation_base = scene_root
        for child in scene_root.children:
            if child.get_type() == 'AnimationPlayer':
                animation_player = child
                break
    else:  # export_settings['animation_modes'] == 'SQUASHED_ACTIONS':
        animation_base = godot_node
        node_ptr = godot_node
        while node_ptr is not None:
            for child in node_ptr.children:
                if child.get_type() == 'AnimationPlayer':
                    animation_player = child
                    break
            if animation_player is not None:
                break
            node_ptr = node_ptr.parent

    if animation_player is None:
        animation_player = AnimationPlayer(
            name='AnimationPlayer',
            parent=animation_base,
        )

        escn_file.add_node(animation_player)

    return animation_player


def blender_path_to_bone_name(blender_object_path):
    """Find the bone name inside a fcurve data path,
    the parameter blender_object_path is part of
    the fcurve.data_path generated through
    split_fcurve_data_path()"""
    return re.search(r'pose.bones\["([^"]+)"\]',
                     blender_object_path).group(1)


def split_fcurve_data_path(data_path):
    """Split fcurve data path into a blender
    object path and an attribute name"""
    path_list = data_path.rsplit('.', 1)

    if len(path_list) == 1:
        return '', path_list[0]
    return path_list[0], path_list[1]


def get_action_frame_range(action):
    """Return the a tuple denoting the frame range of action"""
    # in blender `last_frame` is included, here plus one to make it
    # excluded to fit python convention
    return int(action.frame_range[0]), int(action.frame_range[1]) + 1


def get_fcurve_frame_range(fcurve):
    """Return the a tuple denoting the frame range of fcurve"""
    return int(fcurve.range()[0]), int(fcurve.range()[1]) + 1


def build_const_interp_value_track(track_path, map_func, fcurve):
    """Build a godot value track from a Blender const interpolation fcurve"""
    track = Track('value', track_path)
    track.interp = NEAREST_INTERPOLATION

    if map_func is None:
        for keyframe in fcurve.keyframe_points:
            track.add_frame_data(int(keyframe.co[0]), keyframe.co[1])
    else:
        for keyframe in fcurve.keyframe_points:
            track.add_frame_data(int(keyframe.co[0]), map_func(keyframe.co[1]))

    return track


def build_linear_interp_value_track(track_path, map_func, fcurve):
    """Build a godot value track by evaluate every frame of Blender fcurve"""
    track = Track('value', track_path)

    frame_range = get_fcurve_frame_range(fcurve)
    if map_func is None:
        for frame in range(frame_range[0], frame_range[1]):
            track.add_frame_data(frame, fcurve.evaluate(frame))
    else:
        for frame in range(frame_range[0], frame_range[1]):
            track.add_frame_data(frame, map_func(fcurve.evaluate(frame)))

    return track


def has_object_constraint(blender_object):
    """Return bool indicate if object has constraint"""
    if isinstance(blender_object, bpy.types.Object):
        return True if blender_object.constraints else False
    return False


def has_pose_constraint(blender_object):
    """Return bool indicate if object has pose constraint"""
    if (isinstance(blender_object, bpy.types.Object) and
            isinstance(blender_object.data, bpy.types.Armature)):
        for pose_bone in blender_object.pose.bones:
            if pose_bone.constraints:
                return True
    return False


def bake_constraint_to_action(blender_object, base_action,
                              bake_type, in_place):
    """Bake pose or object constrainst (e.g. IK) to action"""
    if base_action is not None:
        blender_object.animation_data.action = base_action
        frame_range = get_action_frame_range(base_action)
    else:
        frame_range = (1, 250)  # default, can be improved

    # if action_bake_into is None, it would create a new one
    # and baked into it
    if in_place:
        action_bake_into = base_action
    else:
        action_bake_into = None

    do_pose = bake_type == "POSE"
    do_object = not do_pose

    if bpy.app.version <= (2, 79, 0):
        active_obj_backup = bpy.context.scene.objects.active

        # the object to bake is the current active object
        bpy.context.scene.objects.active = blender_object
        baked_action = bpy_extras.anim_utils.bake_action(
            frame_start=frame_range[0],
            frame_end=frame_range[1],
            frame_step=1,
            only_selected=False,
            action=action_bake_into,
            do_pose=do_pose,
            do_object=do_object,
            do_visual_keying=True,
        )

        bpy.context.scene.objects.active = active_obj_backup
    else:
        baked_action = bpy_extras.anim_utils.bake_action(
            obj=blender_object,
            frame_start=frame_range[0],
            frame_end=frame_range[1],
            frame_step=1,
            only_selected=False,
            action=action_bake_into,
            do_pose=do_pose,
            do_object=do_object,
            do_visual_keying=True,
        )

    if in_place:
        return action_bake_into

    if base_action is not None:
        baked_action.name = base_action.name[:-len(BAKING_SUFFIX)]
    else:
        baked_action.name = blender_object.name + 'Action'
    return baked_action


def export_transform_action(godot_node, animation_player,
                            blender_object, action, animation_resource):
    """Export a action with bone and object transform"""

    class TransformFrame:
        """A data structure hold transform values of an animation key,
        it is used as an intermedia data structure, being updated during
        parsing the fcurve data and finally being converted to a transform
        matrix, notice itself uses location, scale, rotation not matrix"""
        ATTRIBUTES = {
            'location', 'scale', 'rotation_quaternion', 'rotation_euler'}

        def __init__(self, default_transform, rotation_mode):
            self.location = default_transform.to_translation()
            # fixme: lose negative scale
            self.scale = default_transform.to_scale()

            # quaternion and euler fcurves may both exist in fcurves
            self.rotation_mode = rotation_mode
            self.rotation_quaternion = default_transform.to_quaternion()
            if rotation_mode == 'QUATERNION':
                self.rotation_euler = default_transform.to_euler()
            else:
                self.rotation_euler = default_transform.to_euler(
                    rotation_mode
                )

        def update(self, attribute, array_index, value):
            """Use fcurve data to update the frame"""
            if attribute == 'location':
                self.location[array_index] = value
            elif attribute == 'scale':
                self.scale[array_index] = value
            elif attribute == 'rotation_quaternion':
                self.rotation_quaternion[array_index] = value
            elif attribute == 'rotation_euler':
                self.rotation_euler[array_index] = value

        def to_matrix(self):
            """Convert location, scale, rotation to a transform matrix"""
            if self.rotation_mode == 'QUATERNION':
                rot_mat = self.rotation_quaternion.to_matrix().to_4x4()
            else:
                rot_mat = self.rotation_euler.to_matrix().to_4x4()
            loc_mat = mathutils.Matrix.Translation(self.location)
            sca_mat = mathutils.Matrix((
                (self.scale[0], 0, 0),
                (0, self.scale[1], 0),
                (0, 0, self.scale[2]),
            )).to_4x4()
            return loc_mat * rot_mat * sca_mat

    def init_transform_frame_values(object_path, blender_object, godot_node,
                                    first_frame, last_frame):
        """Initialize a list of TransformFrame for every animated object"""
        if object_path.startswith('pose'):
            bone_name = blender_path_to_bone_name(object_path)

            # bone fcurve in a non armature object
            if godot_node.get_type() != 'Skeleton':
                logging.warning(
                    "Skip a bone fcurve in a non-armature "
                    "object '%s'",
                    blender_object.name
                )
                return None

            # if the correspond bone of this track not exported, skip
            if godot_node.find_bone_id(bone_name) == -1:
                return None

            pose_bone = blender_object.pose.bones[
                blender_object.pose.bones.find(bone_name)
            ]

            default_frame = TransformFrame(
                pose_bone.matrix_basis,
                pose_bone.rotation_mode
            )
        else:
            # the fcurve location is matrix_basis.to_translation()
            default_frame = TransformFrame(
                blender_object.matrix_basis,
                blender_object.rotation_mode
            )

        return [
            copy.deepcopy(default_frame)
            for _ in range(last_frame - first_frame)
        ]

    first_frame, last_frame = get_action_frame_range(action)

    transform_frame_values_map = collections.OrderedDict()
    for fcurve in action.fcurves:
        # fcurve data are seperated into different channels,
        # for example a transform action would have several fcurves
        # (location.x, location.y, rotation.x ...), so here fcurves
        # are aggregated to object while being evaluted
        object_path, attribute = split_fcurve_data_path(fcurve.data_path)

        if (object_path not in transform_frame_values_map and
                attribute in TransformFrame.ATTRIBUTES):

            frame_values = init_transform_frame_values(
                object_path, blender_object,
                godot_node, first_frame, last_frame
            )

            # unsuccessfully initialize frames, then skip this fcurve
            if not frame_values:
                continue

            transform_frame_values_map[object_path] = frame_values

        if attribute in TransformFrame.ATTRIBUTES:
            for frame in range(first_frame, last_frame):
                transform_frame_values_map[
                    object_path][frame - first_frame].update(
                        attribute,
                        fcurve.array_index,
                        fcurve.evaluate(frame)
                    )

    for object_path, frame_value_list in transform_frame_values_map.items():
        if object_path == '':
            # object_path equals '' represents node itself
            if godot_node.parent.get_type() == 'BoneAttachment':
                transform_mtx_list = [
                    fix_bone_attachment_transform(
                        blender_object,
                        blender_object.matrix_parent_inverse * x.to_matrix
                    ) for x in frame_value_list
                ]
            else:
                transform_mtx_list = [
                    blender_object.matrix_parent_inverse *
                    x.to_matrix() for x in frame_value_list
                ]

            # convert matrix_basis to matrix_local(parent space transform)
            if (godot_node.get_type()
                    in ("SpotLight", "DirectionalLight", "Camera")):
                transform_mtx_list = [
                    fix_directional_transform(mtx)
                    for mtx in transform_mtx_list
                ]

            track_path = NodePath(
                animation_player.parent.get_path(),
                godot_node.get_path()
            )

        elif object_path.startswith('pose'):
            track_path = NodePath(
                animation_player.parent.get_path(),
                godot_node.get_path(),
                godot_node.find_bone_name(
                    blender_path_to_bone_name(object_path)
                ),
            )

            transform_mtx_list = [x.to_matrix() for x in frame_value_list]

        animation_resource.add_track(
            Track(
                'transform',
                track_path,
                range(first_frame, last_frame),
                transform_mtx_list
            )
        )


def export_shapekey_action(godot_node, animation_player,
                           blender_object, action, animation_resource):
    """Export shapekey value action"""
    first_frame, last_frame = get_action_frame_range(action)

    for fcurve in action.fcurves:

        object_path, attribute = split_fcurve_data_path(fcurve.data_path)

        if attribute == 'value':
            shapekey_name = re.search(r'key_blocks\["([^"]+)"\]',
                                      object_path).group(1)

            track_path = NodePath(
                animation_player.parent.get_path(),
                godot_node.get_path(),
                "blend_shapes/{}".format(shapekey_name)
            )

            value_track = Track(
                'value',
                track_path,
            )

            for frame in range(first_frame, last_frame):
                value_track.add_frame_data(frame, fcurve.evaluate(frame))

            animation_resource.add_track(value_track)


def export_light_action(light_node, animation_player,
                        blender_lamp, action, animation_resource):
    """Export light(lamp in Blender) action"""
    first_frame, last_frame = get_action_frame_range(action)
    base_node_path = NodePath(
        animation_player.parent.get_path(), light_node.get_path()
    )

    animation_resource.add_simple_value_track(
        action.fcurves, 'use_negative',
        partial(
            build_const_interp_value_track,
            base_node_path.new_copy('light_negative'),
            lambda x: x > 0.0,
        )
    )

    animation_resource.add_track_via_attr_mapping(
        action.fcurves,
        light_node.attribute_conversion,
        base_node_path
    )

    # color tracks is not one-one mapping to fcurve, they
    # need to be treated like transform track
    color_frame_values_map = collections.OrderedDict()

    for fcurve in action.fcurves:
        _, attribute = split_fcurve_data_path(fcurve.data_path)

        if attribute in ('color', 'shadow_color'):
            if attribute not in color_frame_values_map:
                color_frame_values_map[attribute] = [
                    mathutils.Color()
                    for _ in range(first_frame, last_frame)
                ]
            color_list = color_frame_values_map[attribute]
            for frame in range(first_frame, last_frame):
                color_list[frame - first_frame][
                    fcurve.array_index] = fcurve.evaluate(frame)

    for attribute, frame_value_list in color_frame_values_map.items():
        if attribute == 'color':
            track_path = base_node_path.new_copy('light_color')
        else:
            track_path = base_node_path.new_copy('shadow_color')

        animation_resource.add_track(
            Track(
                'value',
                track_path,
                range(first_frame, last_frame),
                frame_value_list
            )
        )


def export_camera_action(camera_node, animation_player,
                         blender_cam, action, animation_resource):
    """Export camera action"""
    first_frame, last_frame = get_action_frame_range(action)
    base_node_path = NodePath(
        animation_player.parent.get_path(), camera_node.get_path()
    )

    animation_resource.add_track_via_attr_mapping(
        action.fcurves,
        camera_node.attribute_conversion,
        base_node_path
    )

    animation_resource.add_simple_value_track(
        action.fcurves, 'type',
        partial(
            build_const_interp_value_track,
            base_node_path.new_copy('projection'),
            lambda x: 0 if x == 0.0 else 1,
        )
    )

    # blender use sensor_width and f_lens to animate fov
    # while godot directly use fov
    fov_animated = False
    focal_len_list = list()
    sensor_size_list = list()

    if action.fcurves.find('lens') is not None:
        fcurve = action.fcurves.find('lens')
        fov_animated = True
        for frame in range(first_frame, last_frame):
            focal_len_list.append(fcurve.evaluate(frame))
    if action.fcurves.find('sensor_width') is not None:
        fcurve = action.fcurves.find('sensor_width')
        fov_animated = True
        for frame in range(first_frame, last_frame):
            sensor_size_list.append(fcurve.evaluate(frame))

    if fov_animated:
        # export fov track
        if not focal_len_list:
            focal_len_list = [blender_cam.lens
                              for _ in range(first_frame, last_frame)]
        if not sensor_size_list:
            sensor_size_list = [blender_cam.sensor_width
                                for _ in range(first_frame, last_frame)]

        fov_list = list()
        for index, flen in enumerate(focal_len_list):
            fov_list.append(2 * math.degrees(
                math.atan(
                    sensor_size_list[index]/2/flen
                )
            ))

        animation_resource.add_track(Track(
            'value',
            base_node_path.new_copy('fov'),
            range(first_frame, last_frame),
            fov_list
        ))


# ----------------------------------------------


ACTION_EXPORTER_MAP = {
    'transform': export_transform_action,
    'shapekey': export_shapekey_action,
    'light': export_light_action,
    'camera': export_camera_action,
}


def export_animation_data(escn_file, export_settings, godot_node,
                          blender_object, action_type):
    """Export the action and nla_tracks in blender_object.animation_data,
    it will further call the action exporting function in AnimationDataExporter
    given by `func_name`"""
    if not export_settings['use_export_animation']:
        return
    has_obj_cst = has_object_constraint(blender_object)
    has_pose_cst = has_pose_constraint(blender_object)
    need_bake = action_type == 'transform' and (has_obj_cst or has_pose_cst)

    if blender_object.animation_data is None and not need_bake:
        return

    def action_baker(action_to_bake):
        """A quick call to bake OBJECT and POSE action"""
        # note it used variable outside its scope
        if has_obj_cst and has_pose_cst:
            tmp = bake_constraint_to_action(
                blender_object, action_to_bake, "OBJECT", False
            )
            ret = bake_constraint_to_action(
                blender_object, tmp, "POSE", True
            )
        elif has_pose_cst:
            ret = bake_constraint_to_action(
                blender_object, action_to_bake, "POSE", False
            )
        elif has_obj_cst:
            ret = bake_constraint_to_action(
                blender_object, action_to_bake, "POSE", False
            )
        return ret

    animation_player = get_animation_player(
        escn_file, export_settings, godot_node
    )
    exporter_func = ACTION_EXPORTER_MAP[action_type]
    # avoid duplicated export, same actions may exist in different nla_strip
    exported_actions = set()

    # back up active action to reset back after finish exporting
    if blender_object.animation_data:
        active_action = blender_object.animation_data.action
    else:
        active_action = None

    def export_active_action(active_action):
        """Export the active action, if needed would call bake,
        note that active_action maybe None, which means the object
        has constraint and need to bake to action"""
        if need_bake:
            if active_action is not None:
                active_action.name = active_action.name + BAKING_SUFFIX
                exported_actions.add(active_action)
            action_active_to_export = action_baker(active_action)
        else:
            action_active_to_export = active_action

        if animation_player.default_animation is None:
            animation_player.add_default_animation_resource(
                escn_file, action_active_to_export
            )

        exporter_func(
            godot_node, animation_player, blender_object,
            action_active_to_export, animation_player.default_animation
        )

        if need_bake:
            # remove new created action
            bpy.data.actions.remove(action_active_to_export)
            if active_action is not None:
                # set back active action name
                active_action.name = active_action.name[:-len(BAKING_SUFFIX)]

    if (active_action is not None or
            not blender_object.animation_data and need_bake):
        export_active_action(active_action)

    def export_nla_action(nla_action):
        """Export an action in nla_tracks, would call baking if needed"""
        exported_actions.add(nla_action)
        if need_bake:
            nla_action.name = nla_action.name + BAKING_SUFFIX
            # nla_action_to_export is new created, need to be removed later
            nla_action_to_export = action_baker(nla_action)
        else:
            nla_action_to_export = nla_action

        anim_resource = animation_player.create_animation_resource(
            escn_file, nla_action_to_export
        )

        exporter_func(godot_node, animation_player, blender_object,
                      nla_action_to_export, anim_resource)

        if need_bake:
            # remove baked action
            bpy.data.actions.remove(nla_action_to_export)
            nla_action.name = nla_action.name[:-len(BAKING_SUFFIX)]

    # export actions in nla_tracks, each exported to seperate
    # animation resources
    for nla_track in blender_object.animation_data.nla_tracks:
        for nla_strip in nla_track.strips:
            # here make sure no duplicate action exported
            if (nla_strip.action is not None and
                    nla_strip.action not in exported_actions):
                export_nla_action(nla_strip.action)

    if active_action is not None:
        blender_object.animation_data.action = active_action
