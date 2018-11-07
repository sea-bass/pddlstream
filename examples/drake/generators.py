from itertools import product

import numpy as np

from examples.drake.iiwa_utils import open_wsg50_gripper, get_box_grasps
from examples.drake.motion import plan_joint_motion, plan_waypoints_joint_motion, \
    get_extend_fn, interpolate_translation, plan_workspace_motion, get_collision_fn
from examples.drake.utils import get_relative_transform, set_world_pose, set_joint_position, get_body_pose, \
    get_base_body, sample_aabb_placement, get_movable_joints, get_model_name, \
    set_joint_positions, get_box_from_geom, get_parent_joints, exists_colliding_pair, get_model_bodies


def bodies_from_models(mbp, models):
    return {body for model in models for body in get_model_bodies(mbp, model)}


class Pose(object):
    # TODO: unify Pose & Conf?
    def __init__(self, mbp, parent, child, transform):
        self.mbp = mbp
        self.parent = parent # body_frame
        self.child = child # model_index
        self.transform = transform

    @property
    def bodies(self):
        return get_model_bodies(self.mbp, self.child)

    def assign(self, context):
        parent_pose = get_relative_transform(self.mbp, context, self.parent)
        child_pose = parent_pose.multiply(self.transform)
        set_world_pose(self.mbp, context, self.child, child_pose)

    def __repr__(self):
        return '{}({}->{})'.format(self.__class__.__name__, get_model_name(self.mbp, self.child), self.parent.name())


class Conf(object):
    def __init__(self, joints, positions):
        assert len(joints) == len(positions)
        self.joints = joints
        self.positions = tuple(positions)

    @property
    def bodies(self): # TODO: descendants
        return {joint.child_body() for joint in self.joints}

    def assign(self, context):
        for joint, position in zip(self.joints, self.positions):
            set_joint_position(joint, context, position)

    def __repr__(self):
        return '{}({})'.format(self.__class__.__name__, len(self.joints))


class Trajectory(object):
    def __init__(self, path, attachments=[]):
        self.path = tuple(path)
        self.attachments = attachments
        # TODO: store a common set of joints instead

    @property
    def joints(self):
        return self.path[0].joints

    @property
    def bodies(self):
        joint_bodies = {joint.child_body() for joint in self.joints}
        for attachment in self.attachments:
            joint_bodies.update(attachment.bodies)
        return joint_bodies

    def iterate(self, context):
        for conf in self.path[1:]:
            conf.assign(context)
            for attach in self.attachments: # TODO: topological sort
                attach.assign(context)
            yield

    def __repr__(self):
        return '{}({},{})'.format(self.__class__.__name__, len(self.joints), len(self.path))

##################################################

def get_stable_gen(task, context, collisions=True):
    mbp = task.mbp
    world = mbp.world_frame()
    box_from_geom = get_box_from_geom(task.scene_graph)
    fixed = task.fixed_bodies() if collisions else []

    def gen(obj_name, surface):
        obj = mbp.GetModelInstanceByName(obj_name)
        surface_body = mbp.GetBodyByName(surface.body_name, surface.model_index)
        surface_pose = get_body_pose(context, surface_body)
        collision_pairs = set(product(get_model_bodies(mbp, obj), fixed)) # + [surface]

        #object_aabb, object_local = AABBs[obj_name], Isometry3.Identity()
        #surface_aabb, surface_local = AABBs[surface_name], Isometry3.Identity()
        object_aabb, object_local, _ = box_from_geom[int(obj), get_base_body(mbp, obj).name(), 0]
        surface_aabb, surface_local, _ = box_from_geom[int(surface.model_index), surface.body_name, surface.visual_index]
        for surface_from_object in sample_aabb_placement(object_aabb, surface_aabb):
            world_pose = surface_pose.multiply(surface_local).multiply(
                surface_from_object).multiply(object_local.inverse())
            pose = Pose(mbp, world, obj, world_pose)
            pose.assign(context)
            if not exists_colliding_pair(task.diagram, task.diagram_context, task.mbp, task.scene_graph, collision_pairs):
                yield pose,
    return gen


def get_grasp_gen(task):
    mbp = task.mbp
    gripper_frame = get_base_body(mbp, task.gripper).body_frame()
    box_from_geom = get_box_from_geom(task.scene_graph)
    #pitch_range = (0, 0) # Top grasps
    #pitch_range = (np.pi/3, np.pi/3)
    pitch_range = (2*np.pi/5, 2*np.pi/5)
    #pitch_range = (-np.pi/2, np.pi/2)

    def gen(obj_name):
        obj = mbp.GetModelInstanceByName(obj_name)
        #obj_aabb, obj_from_box = AABBs[obj_name], Isometry3.Identity()
        obj_aabb, obj_from_box, _ = box_from_geom[int(obj), get_base_body(mbp, obj).name(), 0]
        #finger_aabb, finger_from_box = box_from_geom[int(task.gripper), 'left_finger', 0]
        # TODO: union of bounding boxes

        #for gripper_from_box in get_top_cylinder_grasps(obj_aabb):
        for gripper_from_box in get_box_grasps(obj_aabb, pitch_range=pitch_range):
            gripper_from_obj = gripper_from_box.multiply(obj_from_box.inverse())
            grasp = Pose(mbp, gripper_frame, obj, gripper_from_obj)
            yield grasp,
    return gen


def get_ik_fn(task, context, collisions=True, max_failures=5, distance=0.1, step_size=0.035):
    #distance = 0.0
    approach_vector = distance*np.array([0, -1, 0])
    gripper_frame = get_base_body(task.mbp, task.gripper).body_frame()
    fixed = task.fixed_bodies() if collisions else []
    initial_guess = None
    #initial_guess = get_joint_positions(joints, context) # TODO: start with initial

    def fn(robot_name, obj_name, pose, grasp):
        # TODO: if gripper/block in collision, return
        robot = task.mbp.GetModelInstanceByName(robot_name)
        joints = get_movable_joints(task.mbp, robot)
        collision_pairs = set(product(bodies_from_models(task.mbp, [robot, task.gripper]), fixed))
        collision_fn = get_collision_fn(task.diagram, task.diagram_context, task.mbp, task.scene_graph,
                                        joints, collision_pairs=collision_pairs) # TODO: while holding

        grasp_pose = pose.transform.multiply(grasp.transform.inverse())
        gripper_path = list(interpolate_translation(grasp_pose, approach_vector, step_size=step_size))

        attempts = 0
        last_success = 0
        while (attempts - last_success) < max_failures:
            attempts += 1
            waypoints = plan_workspace_motion(task.mbp, joints, gripper_frame, gripper_path,
                                              initial_guess=initial_guess, collision_fn=collision_fn)
            if waypoints is None:
                continue
            path = plan_waypoints_joint_motion(joints, waypoints, collision_fn=collision_fn)
            if path is None:
                continue
            #path = refine_joint_path(joints, path)
            traj = Trajectory(Conf(joints, q) for q in path)
            #print(attempts - last_success)
            last_success = attempts
            return traj.path[-1], traj
    return fn


def get_pull_fn(task, context, collisions=True, max_attempts=25, step_size=np.pi / 16):
    box_from_geom = get_box_from_geom(task.scene_graph)
    gripper_frame = get_base_body(task.mbp, task.gripper).body_frame()
    fixed = task.fixed_bodies() if collisions else []
    pitch = np.pi/2 # TODO: can use a different pitch
    grasp_length = 0.02

    #approach_vector = 0.01*np.array([0, -1, 0])
    # TODO: could also push the door either perpendicular or parallel
    # TODO: could solve for kinematic solution of robot and doors
    # DoDifferentialInverseKinematics
    # TODO: allow small rotation error perpendicular to handle

    def fn(robot_name, door_name, dq1, dq2):
        robot = task.mbp.GetModelInstanceByName(robot_name)
        robot_joints = get_movable_joints(task.mbp, robot)
        collision_pairs = set(product(bodies_from_models(task.mbp, [robot, task.gripper]), fixed))
        collision_fn = get_collision_fn(task.diagram, task.diagram_context, task.mbp, task.scene_graph,
                                        robot_joints, collision_pairs=collision_pairs)

        door_body = task.mbp.GetBodyByName(door_name)
        door_joints = dq1.joints

        extend_fn = get_extend_fn(door_joints, resolutions=step_size*np.ones(len(door_joints)))
        door_joint_path = [dq1.positions] + list(extend_fn(dq1.positions, dq2.positions)) # TODO: check for collisions
        door_cartesian_path = []
        for robot_conf in door_joint_path:
            set_joint_positions(door_joints, context, robot_conf)
            door_cartesian_path.append(get_body_pose(context, door_body))

        shape, index = 'cylinder', 1 # Second grasp is np.pi/2, corresponding to +y
        #shape, index = 'box', 0 # left_door TODO: right_door
        for i in range(2):
            handle_aabb, handle_from_box, handle_shape = box_from_geom[int(door_body.model_instance()), door_name, i]
            if handle_shape == shape:
                break
        else:
            raise RuntimeError(shape)
        [gripper_from_box] = list(get_box_grasps(handle_aabb, orientations=[index], pitch_range=(pitch, pitch), grasp_length=grasp_length))
        gripper_from_obj = gripper_from_box.multiply(handle_from_box.inverse())
        pull_cartesian_path = [body_pose.multiply(gripper_from_obj.inverse()) for body_pose in door_cartesian_path]

        #start_path = list(interpolate_translation(pull_cartesian_path[0], approach_vector))
        #end_path = list(interpolate_translation(pull_cartesian_path[-1], approach_vector))
        for _ in range(max_attempts):
            pull_joint_waypoints = plan_workspace_motion(task.mbp, robot_joints, gripper_frame, reversed(pull_cartesian_path),
                                                         collision_fn=collision_fn)
            if pull_joint_waypoints is None:
                continue
            pull_joint_waypoints = pull_joint_waypoints[::-1]
            rq1 = Conf(robot_joints, pull_joint_waypoints[0])
            rq2 = Conf(robot_joints, pull_joint_waypoints[-1])
            combined_joints = robot_joints + door_joints
            combined_waypoints = [list(rq) + list(dq) for rq, dq in zip(pull_joint_waypoints, door_joint_path)]
            pull_joint_path = plan_waypoints_joint_motion(combined_joints, combined_waypoints, collision_fn=lambda q: False)
            if pull_joint_path is None:
                continue
            traj = Trajectory(Conf(combined_joints, combined_conf) for combined_conf in pull_joint_path)
            return rq1, rq2, traj
    return fn

##################################################

def get_motion_fn(task, context, collisions=True):
    gripper = task.gripper

    def fn(robot_name, conf1, conf2, fluents=[]):
        robot = task.mbp.GetModelInstanceByName(robot_name)
        joints = get_movable_joints(task.mbp, robot)

        moving = bodies_from_models(task.mbp, [robot, gripper])
        obstacles = set(task.fixed_bodies())
        attachments = []
        for fact in fluents:
            predicate = fact[0]
            if predicate == 'atconf':
                name, conf = fact[1:]
                conf.assign(context)
                obstacles.update(conf.bodies)
            elif predicate == 'atpose':
                name, pose = fact[1:]
                pose.assign(context)
                obstacles.update(pose.bodies)
            elif predicate == 'atgrasp':
                robot, name, grasp = fact[1:]
                attachments.append(grasp)
                moving.update(grasp.bodies)
            else:
                raise ValueError(predicate)

        obstacles -= moving
        #print(sorted(body.name() for body in moving))
        #print(sorted(body.name() for body in obstacles))
        #print(attachments)
        # Can make separate predicate for the frame something is in at a particular time
        collision_pairs = set(product(moving, obstacles)) if collisions else set()
        collision_fn = get_collision_fn(task.diagram, task.diagram_context, task.mbp, task.scene_graph,
                                        joints, collision_pairs=collision_pairs, attachments=attachments)

        open_wsg50_gripper(task.mbp, context, gripper)
        set_joint_positions(joints, context, conf1.positions)
        path = plan_joint_motion(joints, conf1.positions, conf2.positions, collision_fn=collision_fn,
                                 restarts=7, iterations=75, smooth=100)
        if path is None:
            return None
        #path = refine_joint_path(joints, path)
        traj = Trajectory(Conf(joints, q) for q in path)
        return traj,
    return fn


def get_collision_test(task, context, collisions=True):
    # TODO: precompute and hash?
    def test(traj, obj_name, pose):
        if not collisions:
            return False
        moving = bodies_from_models(task.mbp, [task.robot, task.gripper])
        moving.update(traj.bodies)
        obstacles = set(pose.bodies) - moving
        collision_pairs = set(product(moving, obstacles))
        if not collision_pairs:
            return False
        pose.assign(context)
        for _ in traj.iterate(context):
            if exists_colliding_pair(task.diagram, task.diagram_context, task.mbp, task.scene_graph, collision_pairs):
                return True
        return False
    return test