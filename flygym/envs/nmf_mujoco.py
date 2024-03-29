import numpy as np
import yaml
import imageio
import copy
import logging
from typing import List, Tuple, Dict, Any, Optional
from pathlib import Path
from scipy.spatial.transform import Rotation as R

import gymnasium as gym
from gymnasium import spaces
from gymnasium.core import ObsType

try:
    import mujoco
    import dm_control
    from dm_control import mjcf
    from dm_control.utils import transformations
except ImportError:
    raise ImportError(
        'MuJoCo prerequisites not installed. Please install the prerequisites '
        'by running `pip install flygym[mujoco]` or '
        '`pip install -e ."[mujoco]"` if installing locally.'
    )

from flygym.terrain.mujoco_terrain import \
    FlatTerrain, Ball, GappedTerrain, ExtrudingBlocksTerrain
from flygym.util.data import mujoco_groundwalking_model_path
from flygym.util.data import default_pose_path, stretch_pose_path
from flygym.util.config import all_leg_dofs


_init_pose_lookup = {
    'default': default_pose_path,
    'stretch': stretch_pose_path,
}
_default_terrain_config = {
    'flat': {
        'size': (50_000, 50_000),
        'friction': (1, 0.005, 0.0001),
        'fly_pos': (0, 0, 300),
        'fly_orient': (0, 1, 0, 0.1)
    },
    'gapped': {
        'x_range': (-10_000, 10_000),
        'y_range': (-10_000, 10_000),
        'friction': (1, 0.005, 0.0001),
        'gap_width': 200,
        'block_width': 1000,
        'gap_depth': 2000,
        'fly_pos': (0, 0, 600),
        'fly_orient': (0, 1, 0, 0.1)
    },
    'blocks': {
        'x_range': (-10_000, 10_000),
        'y_range': (-10_000, 10_000),
        'friction': (1, 0.005, 0.0001),
        'block_size': 1000,
        'height_range': (300, 300),
        'rand_seed': 0,
        'fly_pos': (0, 0, 600),
        'fly_orient': (0, 1, 0, 0.1)
    },
    'ball': {
        'radius': ...,
        'fly_pos': (0, 0, ...),
        'fly_orient': (0, 1, 0, ...),
    },
}
_default_physics_config = {
    'joint_stiffness': 2500,
    'friction': (1, 0.005, 0.0001),
    'gravity': (0, 0, -9.81e5),
}
_default_render_config = {
    'saved': {'window_size': (640, 480), 'playspeed': 1.0, 'fps': 60},
    'headless': {}
}
    

class NeuroMechFlyMuJoCo(gym.Env):
    """A NeuroMechFly environment using MuJoCo as the physics engine.

    Attributes
    ----------
    render_mode : str
        The rendering mode. Can be 'headless' (no graphic rendering),
        'viewer' (display rendered images as the simulation takes
        place), or 'saved' (saving the rendered video to a file under
        ``output_dir`` at the end of the simulation).
    render_config : Dict[str, Any]
        Rendering configuration. Allowed parameters depend on the
        rendering mode (``render_mode``).
    actuated_joints : List[str]
        List of actuated joints.
    timestep : float
        Simulation timestep in seconds.
    output_dir : Path
        Directory to save simulation data.
    terrain : str
        The terrain type. Can be 'flat' or 'ball'.
    terrain_config : Dict[str, Any]
        Terrain configuration. Allowed parameters depend on the terrain
        type (``terrain``).
    physics_config : Dict[str, Any]
        Physics configuration (gravity, joint stiffness, etc).
    control : str
        The joint controller type. Can be 'position', 'velocity', or
        'torque'.
    init_pose : str
        Which initial pose to start the simulation from. Currently only
        'default' is implemented.
    action_space : Dict[str, gym.spaces.Box]
        Definition of the simulation's action space as a Gym
        environment.
    observation_space : Dict[str, gym.spaces.Box]
        Definition of the simulation's observation space as a Gym
        environment.
    model : dm_control.mjcf.RootElement
        The MuJoCo model.
    physics : dm_control.mujoco.Physics
        The MuJoCo physics simulation.
    actuators : Dict[str, dm_control.mjcf.Element]
        The MuJoCo actuators.
    joint_sensors : Dict[str, dm_control.mjcf.Element]
        The MuJoCo sensors on joint positions, velocities, and forces.
    body_sensors : Dict[str, dm_control.mjcf.Element]
        The MuJoCo sensors on thorax position and orientation.
    curr_time : float
        The (simulated) time elapsed since the last reset (in seconds).
    
    """
    _metadata = {
        'render_modes': ['headless', 'viewer', 'saved'],
        'terrain': ['flat', 'gapped', 'blocks', 'ball'],
        'control': ['position', 'velocity', 'torque'],
        'init_pose': ['default', 'stretch']
    }
    
    def __init__(self,
                 render_mode: str = 'saved',
                 render_config: Dict[str, Any] = {},
                 actuated_joints: List = all_leg_dofs,
                 timestep: float = 0.0001,
                 output_dir: Optional[Path] = None,
                 terrain: str = 'flat',
                 terrain_config: Dict[str, Any] = {},
                 physics_config: Dict[str, Any] = {},
                 control: str = 'position',
                 init_pose: str = 'default',
                 ) -> None:
        """Initialize a MuJoCo-based NeuroMechFly environment.

        Parameters
        ----------
        render_mode : str, optional
            The rendering mode. Can be 'headless' (no graphic rendering),
            'viewer' (display rendered images as the simulation takes
            place), or 'saved' (saving the rendered video to a file under
            ``output_dir`` at the end of the simulation). By default
            'saved'.
        render_config : Dict[str, Any], optional
            Rendering configuration. Allowed parameters depend on the
            rendering mode (``render_mode``). See :ref:`mujoco_config`
            for detailed options.
        actuated_joints : List, optional
            List of actuated joint DoFs, by default all leg DoFs
        timestep : float, optional
            Simulation timestep in seconds, by default 0.0001
        output_dir : Path, optional
            Directory to save simulation data (by default just the video,
            but you can extend this class to save additional data).
            If ``None``, no data will be saved. By default None
        terrain : str, optional
            The terrain type. Can be 'flat' or 'ball'. By default 'flat'
        terrain_config : Dict[str, Any], optional
            Terrain configuration. Allowed parameters depend on the
            terrain type. See :ref:`mujoco_config` for detailed options.
        physics_config : Dict[str, Any], optional
            Physics configuration (gravity, joint stiffness, etc). See
            :ref:`mujoco_config` for detailed options.
        control : str, optional
            The joint controller type. Can be 'position', 'velocity', or
            'torque'., by default 'position'
        init_pose : str, optional
            Which initial pose to start the simulation from. Currently only
            'default' is implemented.
        """
        self.render_mode = render_mode
        self.render_config = copy.deepcopy(_default_render_config[render_mode])
        self.render_config.update(render_config)
        self.actuated_joints = actuated_joints
        self.timestep = timestep
        if output_dir is not None:
            output_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir = output_dir
        self.terrain = terrain
        self.terrain_config = copy.deepcopy(_default_terrain_config[terrain])
        self.terrain_config.update(terrain_config)
        self.physics_config = copy.deepcopy(_default_physics_config)
        self.physics_config.update(physics_config)
        self.control = control
        
        # Define action and observation spaces
        num_dofs = len(actuated_joints)
        bound = np.pi if control == 'position' else np.inf
        self.action_space = {
            'joints': spaces.Box(low=-bound, high=bound, shape=(num_dofs,))
        }
        self.observation_space = {
            # joints: shape (3, num_dofs): (pos, vel, torque) of each DoF
            'joints': spaces.Box(low=-np.inf, high=np.inf,
                                 shape=(3, num_dofs)),
            # fly: shape (4, 3):
            # 0th row: x, y, z position of the fly in arena
            # 1st row: x, y, z velocity of the fly in arena
            # 2nd row: orientation of fly around x, y, z axes
            # 3rd row: rate of change of fly orientation
            'fly': spaces.Box(low=-np.inf, high=np.inf, shape=(4, 3)),
        }
        
        # Load NMF model
        self.model = mjcf.from_path(mujoco_groundwalking_model_path)
        self.model.option.timestep = timestep
        if init_pose not in self._metadata['init_pose']:
            raise ValueError(f'Invalid init_pose: {init_pose}')
        with open(_init_pose_lookup[init_pose]) as f:
            init_pose = {k: np.deg2rad(v)
                         for k, v in yaml.safe_load(f)['joints'].items()}
        self.init_pose = {k: v for k, v in init_pose.items()
                          if k in actuated_joints}
        
        # Fix unactuated joints and define list of actuated joints
        # for joint in model.find_all('joint'):
        #     if joint.name not in actuated_joints:
        #         joint.type = 'fixed'
        self.actuators = [
            self.model.find('actuator', f'actuator_{control}_{joint}')
            for joint in actuated_joints
        ]
        
        # Add sensors
        self.joint_sensors = []
        for joint in actuated_joints:
            self.joint_sensors.extend([
                self.model.sensor.add('jointpos',
                                 name=f'jointpos_{joint}', joint=joint),
                self.model.sensor.add('jointvel',
                                 name=f'jointvel_{joint}', joint=joint),
                self.model.sensor.add('actuatorfrc',
                                 name=f'actuatorfrc_position_{joint}',
                                 actuator=f'actuator_position_{joint}'),
                self.model.sensor.add('actuatorfrc',
                                 name=f'actuatorfrc_velocity_{joint}',
                                 actuator=f'actuator_velocity_{joint}'),
                self.model.sensor.add('actuatorfrc',
                                 name=f'actuatorfrc_motor_{joint}',
                                 actuator=f'actuator_torque_{joint}'),
            ])
        self.body_sensors = [
            self.model.sensor.add('framepos', name='thorax_pos',
                             objtype='body', objname='Thorax'),
            self.model.sensor.add('framelinvel', name='thorax_linvel',
                             objtype='body', objname='Thorax'),
            self.model.sensor.add('framequat', name='thorax_quat',
                             objtype='body', objname='Thorax'),
            self.model.sensor.add('frameangvel', name='thorax_angvel',
                             objtype='body', objname='Thorax')
        ]
        
        # Add arena and put fly in it
        if terrain == 'flat':
            my_terrain = FlatTerrain(
                size=self.terrain_config['size'],
                friction=self.terrain_config['friction']
            )
            my_terrain.spawn_entity(self.model,
                                    rel_pos=self.terrain_config['fly_pos'],
                                    rel_angle=self.terrain_config['fly_orient'])
            arena = my_terrain.arena
        elif terrain == 'gapped':
            my_terrain = GappedTerrain(
                x_range=self.terrain_config['x_range'],
                y_range=self.terrain_config['y_range'],
                gap_width=self.terrain_config['gap_width'],
                block_width=self.terrain_config['block_width'],
                gap_depth=self.terrain_config['gap_depth'],
                friction=self.terrain_config['friction']
            )
            my_terrain.spawn_entity(self.model,
                                    rel_pos=self.terrain_config['fly_pos'],
                                    rel_angle=self.terrain_config['fly_orient'])
            arena = my_terrain.arena
        elif terrain == 'blocks':
            my_terrain = ExtrudingBlocksTerrain(
                x_range=self.terrain_config['x_range'],
                y_range=self.terrain_config['y_range'],
                block_size=self.terrain_config['block_size'],
                height_range=self.terrain_config['height_range'],
                rand_seed=self.terrain_config['rand_seed'],
                friction=self.terrain_config['friction']
            )
            my_terrain.spawn_entity(self.model,
                                    rel_pos=self.terrain_config['fly_pos'],
                                    rel_angle=self.terrain_config['fly_orient'])
            arena = my_terrain.arena
        elif terrain == 'ball':
            raise NotImplementedError
        
        arena.option.timestep = timestep
        self.physics = mjcf.Physics.from_mjcf_model(arena)
        self.curr_time = 0
        self._last_render_time = -np.inf
        if render_mode != 'headless':        
            self._eff_render_interval = (self.render_config['playspeed'] /
                                         self.render_config['fps'])
        self._frames = []
        
        # Ad hoc changes to gravity, stiffness, and friction
        for geom in [geom.name for geom in arena.find_all('geom')]:
            if 'collision' in geom:
                self.physics.model.geom(f'Animat/{geom}').friction = \
                    self.physics_config['friction']
        
        for joint in self.actuated_joints:
            if joint is not None:
                self.physics.model.joint(f'Animat/{joint}').stiffness = \
                    self.physics_config['joint_stiffness']
        
        self.physics.model.opt.gravity = self.physics_config['gravity']
        
        # set complaint tarsus
        all_joints = [joint.name for joint in arena.find_all('joint')]
        self._set_compliant_Tarsus(all_joints, kp=5.0, stiff=0.0)
        # set init pose
        self._set_init_pose(self.init_pose)
            
    
    def _set_init_pose(self, init_pose: Dict[str, float]):
        with self.physics.reset_context():
            for i in range(len(self.actuated_joints)):
                if ((self.actuators[i].joint.name in self.actuated_joints) and
                        (self.actuators[i].joint.name in init_pose)):
                    angle_0 = init_pose[self.actuators[i].joint.name]
                    self.physics.named.data.qpos[
                        f'Animat/{self.actuators[i].joint.name}'
                    ] = angle_0
    
    
    def _set_compliant_Tarsus(self,
                              all_joints: List,
                              kp: float = 5,
                              stiff: float = 0.0,
                              damping: float = 100):
        """Set the Tarsus2/3/4/5 to be compliant by setting the kp
        stifness and damping to a low value"""
        for actuator in self.actuators:
            if (('position' in actuator.name) and
                    ('Tarsus' in actuator.name) and
                    (not 'Tarsus1' in actuator.name)):
                self.physics.model.actuator(
                    f'Animat/{actuator.name}'
                ).gainprm[0] = kp

        for joint in all_joints:
            if joint is None:
                continue
            if ('Tarsus' in joint) and (not 'Tarsus1' in joint):
                self.physics.model.joint(f'Animat/{joint}').stiffness = stiff
                self.physics.model.joint(f'Animat/{joint}').damping = damping
        
        self.physics.reset()
                    
                    
    def reset(self) -> Tuple[ObsType, Dict[str, Any]]:
        """Reset the Gym environment.

        Returns
        -------
        ObsType
            The observation as defined by the environment.
        Dict[str, Any]
            Any additional information that is not part of the
            observation. This is an empty dictionary by default but
            the user can override this method to return additional
            information.
        """
        self.physics.reset()
        self.curr_time = 0
        self._set_init_pose(self.init_pose)
        self._frames = []
        self._last_render_time = -np.inf
        return self._get_observation(), self._get_info()
    
    
    def step(self, action: ObsType
             ) -> Tuple[ObsType, float, bool, Dict[str, Any]]:
        """Step the Gym environment.

        Parameters
        ----------
        action : ObsType
            Action dictionary as defined by the environment's
            action space.

        Returns
        -------
        ObsType
            The observation as defined by the environment.
        Dict[str, Any]
            Any additional information that is not part of the
            observation. This is an empty dictionary by default but
            the user can override this method to return additional
            information.
        """
        self.physics.bind(self.actuators).ctrl = action['joints']
        self.physics.step()
        self.curr_time += self.timestep
        return self._get_observation(), self._get_info()
    
    
    def render(self):
        """Call the ``render`` method to update the renderer. It should
        be called every iteration; the method will decide by itself
        whether action is required."""
        if self.render_mode  == 'headless':
            return
        if self.curr_time < self._last_render_time + self._eff_render_interval:
            return
        if self.render_mode == 'saved':
            width, height = self.render_config['window_size']
            img = self.physics.render(width=width, height=height)
            self._frames.append(img.copy())
            self._last_render_time = self.curr_time
        else:
            raise NotImplementedError
    
    
    def _get_observation(self) -> Tuple[ObsType, Dict[str, Any]]:
        # joint sensors
        joint_obs = np.zeros((3, len(self.actuated_joints)))
        joint_sensordata = self.physics.bind(self.joint_sensors).sensordata
        for i, joint in enumerate(self.actuated_joints):
            base_idx = i * 5
            # pos and vel
            joint_obs[:2, i] = joint_sensordata[base_idx:base_idx + 2]
            # torque from pos/vel/motor actuators
            joint_obs[2, i] = joint_sensordata[base_idx + 2:base_idx + 5].sum()
        joint_obs[2, :] *= 1e-9  # convert to N
        
        # fly position and orientation
        cart_pos = self.physics.bind(self.body_sensors[0]).sensordata
        cart_vel = self.physics.bind(self.body_sensors[1]).sensordata
        quat = self.physics.bind(self.body_sensors[2]).sensordata
        # ang_pos = transformations.quat_to_euler(quat)
        ang_pos = R.from_quat(quat).as_euler('xyz')  # explicitly use intrinsic
        ang_pos[0] *= -1  # flip roll??
        ang_vel = self.physics.bind(self.body_sensors[3]).sensordata
        fly_pos = np.array([cart_pos, cart_vel, ang_pos, ang_vel])
         
        return {
            'joints': joint_obs,
            'fly': fly_pos,
        }
    
    
    def _get_info(self):
        return {}
    
    def save_video(self, path: Path):
        """Save rendered video since the beginning or the last
        ``reset()``, whichever is the latest.
        Only useful if ``render_mode`` is 'saved'.

        Parameters
        ----------
        path : Path
            Path to which the video should be saved.
        """
        if self.render_mode != 'saved':
            logging.warning('Render mode is not "saved"; no video will be '
                            'saved despite `save_video()` call.')
        
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        logging.info(f'Saving video to {path}')
        with imageio.get_writer(path, fps=self.render_config['fps']) as writer:
            for frame in self._frames:
                writer.append_data(frame)
    
    
    def close(self):
        """Close the environment, save data, and release any resources."""
        if self.render_mode == 'saved' and self.output_dir is not None:
            self.save_video(self.output_dir / 'video.mp4')