import os
from glob import glob

from setuptools import setup

package_name = "pid_controller"

setup(
    name=package_name,
    version="2.0.0",
    packages=[package_name],
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml"]),
        (os.path.join("share", package_name, "launch"), glob("launch/*.launch.py")),
        (os.path.join("share", package_name, "config", "pid_controller_node"), glob("config/pid_controller_node/*.yaml")),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="Liam",
    maintainer_email="liam@duckietown.com",
    description="A basic PID controller (heading and lateral offset) for the Duckiebot.",
    license="Duckietown License 2.0",
    entry_points={
        "console_scripts": [
            "pid_controller_node = pid_controller.pid_controller_node:main",
        ],
    },
)
