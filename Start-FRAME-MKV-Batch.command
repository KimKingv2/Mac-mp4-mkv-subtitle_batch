#!/bin/bash
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR" || exit 1
python3 "$SCRIPT_DIR/frame_mkv_batch.py"
STATUS=$?
echo
if [ "$STATUS" -ne 0 ]; then
  echo "工具結束，狀態碼：$STATUS"
fi
read -r -p "按 Enter 關閉視窗……"
exit "$STATUS"
