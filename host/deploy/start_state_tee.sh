#!/bin/bash
# Bring up the state tee on the perception board (192.168.1.103).
#
# The dog's jy_exe always sends its state stream to 192.168.1.103:43897. This
# script puts a relay in that slot and moves the vendor ROS bridge one port up,
# so both the vendor topics and our own consumers see the same stream:
#
#   dog:34770 --> 43897 state_relay --> 127.0.0.1:43896 qnx2ros (ROS topics)
#                                   \-> 192.168.1.104:43900 dev PC
#                                   \-> 192.168.1.100:43900 ZYNQ-7020
#
# Idempotent: safe to re-run. Run as user ysc on the board.
# Rollback: ./stop_state_tee.sh
set -u

BASE=/home/ysc/robot_state_relay
ROS_SETUP="source /opt/ros/melodic/setup.bash; source /home/ysc/message_transformer_ws/devel/setup.bash; export ROS_MASTER_URI=http://127.0.0.1:11311"
PC_IP="${PC_IP:-192.168.1.102}"   # DHCP：10-27 是 .104，11-01 变成 .102，可用环境变量覆盖
ZYNQ_IP=192.168.1.100
PORT=43900

cd "$BASE" || exit 1

echo "[1/4] stopping any previous instances"
pkill -f "state_relay.py --listen" >/dev/null 2>&1
rosnode list 2>/dev/null | grep -q '^/qnx2ros$' && rosnode kill /qnx2ros >/dev/null 2>&1
sleep 1
pkill -f "message_transformer/qnx2ros" >/dev/null 2>&1
sleep 1

echo "[2/4] starting relay on :43897 -> 127.0.0.1:43896, ${PC_IP}:${PORT}, ${ZYNQ_IP}:${PORT}"
setsid nohup python3 state_relay.py --listen 43897 --ros 127.0.0.1:43896 \
    --forward "${PC_IP}:${PORT}" --forward "${ZYNQ_IP}:${PORT}" \
    --log "$BASE/state_relay.log" >> "$BASE/relay_stdout.log" 2>&1 < /dev/null &
sleep 1

echo "[3/4] starting vendor qnx2ros on :43896"
bash -c "$ROS_SETUP; setsid nohup rosrun message_transformer qnx2ros _server_port:=43896 _filter_size:=1 _is_vel_world:=true _is_imu_pub:=true >> $BASE/qnx2ros.log 2>&1 < /dev/null &"
sleep 4

echo "[4/4] verification"
echo "--- listening sockets ---"
ss -ulpn 2>/dev/null | grep -E '4389[0-9]'
echo "--- ros nodes ---"
bash -c "$ROS_SETUP; rosnode list" 2>/dev/null
echo "--- leg_odom rate (3 s) ---"
bash -c "$ROS_SETUP; timeout 4 rostopic hz /leg_odom" 2>/dev/null | head -3
echo "--- relay tail ---"
tail -2 "$BASE/state_relay.log" 2>/dev/null
