#!/bin/bash
# Roll the perception board back to the factory layout:
#   dog:34770 --> 192.168.1.103:43897 qnx2ros (vendor ROS bridge), as shipped.
# Use this if the tee ever misbehaves during a demo.
set -u

BASE=/home/ysc/robot_state_relay
ROS_SETUP="source /opt/ros/melodic/setup.bash; source /home/ysc/message_transformer_ws/devel/setup.bash; export ROS_MASTER_URI=http://127.0.0.1:11311"

echo "[1/3] stopping relay and the relocated qnx2ros"
pkill -f "state_relay.py --listen" >/dev/null 2>&1
bash -c "$ROS_SETUP; rosnode kill /qnx2ros" >/dev/null 2>&1
sleep 1
pkill -f "message_transformer/qnx2ros" >/dev/null 2>&1
sleep 1

echo "[2/3] relaunching the vendor launch file (qnx2ros back on :43897)"
bash -c "cd /home/ysc/message_transformer_ws; $ROS_SETUP; setsid nohup roslaunch message_transformer message_transformer.launch >> $BASE/vendor_roslaunch.log 2>&1 < /dev/null &"
sleep 6

echo "[3/3] verification"
ss -ulpn 2>/dev/null | grep -E '4389[0-9]'
bash -c "$ROS_SETUP; rosnode list" 2>/dev/null
