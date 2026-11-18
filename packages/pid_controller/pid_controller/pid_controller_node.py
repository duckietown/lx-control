#!/usr/bin/env python3
"""PID controller node of the Control learning experience.

The controller itself (``HeadingControl`` / ``OffsetControl``) is the student's code in
``packages/solution/pid_controller.py``. This node feeds it with the pose estimated by the
``encoder_pose_node`` and publishes the resulting car command. Everything the student tunes
is a ROS 2 parameter or a service, driven from the notebook panels:

    parameters : activity (pid_heading | pid_offset), kp, ki, kd, v_ref, heading_ref [deg], offset_ref [m]
    services   : ~/start, ~/stop (std_srvs/Trigger)
"""
import math

import numpy as np
import rclpy
from duckietown_msgs.msg import Twist2DStamped
from nav_msgs.msg import Odometry
from rclpy.node import Node
from std_srvs.srv import Trigger

from lx_ros2_tools.params import LiveParams
from solution.pid_controller import PIDController

ACTIVITIES = ("pid_heading", "pid_offset")


def quaternion_to_yaw(x: float, y: float, z: float, w: float) -> float:
    """Yaw (rotation about z) of a quaternion, in radians."""
    return math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))


class PIDControllerNode(Node):
    """
    Runs the student's PID controller on the pose estimated from the wheel encoders.

    Parameters (live, see config/pid_controller_node/default.yaml):
        activity (str): "pid_heading" or "pid_offset"
        kp, ki, kd (float): PID gains
        v_ref (float): fixed linear velocity [m/s]
        heading_ref (float): reference heading [deg] (pid_heading)
        offset_ref (float): reference lateral position y [m] (pid_offset)
    Publisher:
        car_cmd_switch_node/cmd (duckietown_msgs/Twist2DStamped): the car command (v, omega)
    Subscribers:
        encoder_pose_node/pose (nav_msgs/Odometry): the estimated pose
    Services:
        ~/start, ~/stop (std_srvs/Trigger): start / emergency-stop the controller
    """

    def __init__(self):
        super().__init__("pid_controller_node")
        self.get_logger().info("Initializing...")
        # get the name of the robot
        self.veh = self.get_namespace().strip("/")
        self.controller = PIDController()
        self.time_now: float = 0.0
        self.time_last_step: float = 0.0

        # current estimate (updated by the pose callback)
        self.y_curr: float = 0.0
        self.theta_curr: float = 0.0
        # last omega command
        self.omega = 0.0

        # the controller runs only between `start` and `stop`
        self.running = False

        # parameters: references, speed and gains (set from the notebook panel / ros2 param set)
        self.params = LiveParams(
            self,
            {
                "activity": "pid_heading",
                "kp": 0.0,
                "ki": 0.0,
                "kd": 0.0,
                "v_ref": 0.0,
                "heading_ref": 0.0,  # [deg]
                "offset_ref": 0.0,  # [m]
            },
            on_change=self.cbParam,
        )
        self.controller.SetGains(kp=float(self.params.kp), ki=float(self.params.ki), kd=float(self.params.kd))

        # Defining subscribers:
        self.create_subscription(Odometry, "encoder_pose_node/pose", self.cbPose, 10)

        # Command publisher
        self.pub_car_cmd = self.create_publisher(Twist2DStamped, "car_cmd_switch_node/cmd", 10)

        # services driven from the notebook panels (ActionButtons)
        self.create_service(Trigger, "~/start", self.srv_start)
        self.create_service(Trigger, "~/stop", self.srv_stop)

        # the odometry is reset every time we STOP (as in the kinematics LX)
        self.reset_pose_client = self.create_client(Trigger, "encoder_pose_node/reset")

        self.get_logger().info(f"Initialized (activity: {self.params.activity}).")

    # --- properties derived from the parameters ----------------------------------------------
    @property
    def PID_HEADING(self) -> bool:
        return self.params.activity == "pid_heading"

    @property
    def PID_OFFSET(self) -> bool:
        return self.params.activity == "pid_offset"

    @property
    def v_0(self) -> float:
        """fixed robot linear velocity"""
        return float(self.params.v_ref)

    @property
    def y_ref(self) -> float:
        """reference y for the PID lateral control activity"""
        return float(self.params.offset_ref)

    @property
    def theta_ref(self) -> float:
        """reference heading [rad] for the heading control activity"""
        return float(np.deg2rad(self.params.heading_ref))

    # --- parameters / services ---------------------------------------------------------------
    def cbParam(self, name, value):
        """A parameter changed (from the notebook panel or `ros2 param set`)."""
        self.get_logger().info(f"parameter {name} = {value}")
        if name in ("kp", "ki", "kd"):
            # update the controller gains
            self.controller.SetGains(kp=float(self.params.kp), ki=float(self.params.ki), kd=float(self.params.kd))
        elif name == "activity":
            self.cbActivity(value)

    def cbActivity(self, activity: str):
        """
        A new activity was selected: stop the robot and reset the odometry.
        """
        self.publishCmd(0.0, 0.0)
        self.running = False
        self.resetPose()
        if activity not in ACTIVITIES:
            self.get_logger().warn(f"unknown activity {activity!r}, expected one of {ACTIVITIES}")
        self.get_logger().info(f"Received activity {activity}")

    def srv_start(self, _req, res):
        if not (self.PID_HEADING or self.PID_OFFSET):
            res.success, res.message = False, f"set the parameter 'activity' to one of {ACTIVITIES} first"
            return res
        self.time_last_step = self.time_now
        self.running = True
        self.get_logger().info(f"START ({self.params.activity}, v_ref={self.v_0})")
        res.success, res.message = True, f"started ({self.params.activity})"
        return res

    def srv_stop(self, _req, res):
        # Emergency stop: zero command, controller off, odometry reset
        self.running = False
        self.publishCmd(0.0, 0.0)
        self.resetPose()
        self.get_logger().info("STOP")
        res.success, res.message = True, "stopped"
        return res

    def resetPose(self):
        """Ask the encoder_pose_node to reset the odometry (non-blocking)."""
        if self.reset_pose_client.service_is_ready():
            self.reset_pose_client.call_async(Trigger.Request())
        else:
            self.get_logger().warn("encoder_pose_node/reset not available, odometry not reset")

    # --- control loop ------------------------------------------------------------------------
    def cbPose(self, pose_msg: Odometry):
        """
        Args:
            pose_msg: pose as an Odometry message

        Returns:
            publishes control actions
        """
        self.y_curr = pose_msg.pose.pose.position.y
        q = pose_msg.pose.pose.orientation
        # We are calling theta the heading here (not the normal psi)
        self.theta_curr = quaternion_to_yaw(q.x, q.y, q.z, q.w)

        # update time
        stamp = pose_msg.header.stamp
        self.time_now = max(self.time_now, stamp.sec + stamp.nanosec * 1e-9)

        # run the controller only when started and in appropriate activities
        if self.running and (self.PID_HEADING or self.PID_OFFSET):
            self.Controller()

    def Controller(self):
        """
        Calculate theta and perform the control actions given by the PID
        """
        delta_time = self.time_now - self.time_last_step
        self.time_last_step = self.time_now

        if self.PID_HEADING:
            v, omega = self.controller.HeadingControl(self.v_0, self.theta_ref, self.theta_curr, delta_time)

        elif self.PID_OFFSET:
            v, omega = self.controller.OffsetControl(self.v_0, self.y_ref, self.y_curr, delta_time)
        else:
            v, omega = 0.0, 0.0

        self.publishCmd(v, omega)

    def publishCmd(self, v, omega):
        """
        Publishes a car command message.

        Args:
            v       (:obj:`double`): linear velocity
            omega   (:obj:`double`): angular velocity
        """
        car_control_msg = Twist2DStamped()
        car_control_msg.header.stamp = self.get_clock().now().to_msg()

        car_control_msg.v = float(v)
        car_control_msg.omega = float(omega)
        # save omega in case of STOP
        self.omega = omega

        self.pub_car_cmd.publish(car_control_msg)


def main(args=None):
    rclpy.init(args=args)
    node = PIDControllerNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
