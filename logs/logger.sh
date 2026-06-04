#!/bin/bash


CURRENT_DIR=$(pwd)
ERROR_LOG="$CURRENT_DIR/logs/errors.log"
CLOCK_LOG="$CURRENT_DIR/logs/clock.log"
CLOCKS_LOG="$CURRENT_DIR/logs/clocks.log"
START_TIME=$(date +%s)

mkdir -p "$CURRENT_DIR/logs"
echo "0" > "$CLOCK_LOG"
echo "false" > "$ERROR_LOG"
: > "$CLOCKS_LOG"

# Per-run max trackers (populated by discover + updated in sampling loop)
declare -A CPU_MAX CCX_MAX CPU_TO_CCX CCX_CORES

discover_topology() {
  local n
  n=$(nproc 2>/dev/null || echo 1)
  local -A l3_to_ccx=()
  local ccx_id=0
  local cpu shared key
  for ((cpu=0; cpu<n; cpu++)); do
    shared=""
    # Prefer the L3 cache shared list (index3 on most AMD systems)
    if [[ -r "/sys/devices/system/cpu/cpu${cpu}/cache/index3/shared_cpu_list" ]]; then
      shared=$(<"/sys/devices/system/cpu/cpu${cpu}/cache/index3/shared_cpu_list")
    else
      # Fallback: pick the last index* that contains a comma-separated list (L3)
      for idx in /sys/devices/system/cpu/cpu${cpu}/cache/index*/shared_cpu_list; do
        [[ -r "$idx" ]] || continue
        local val
        val=$(<"$idx")
        if [[ "$val" == *","* ]]; then
          shared="$val"
        fi
      done
    fi
    [[ -z "$shared" ]] && shared="$cpu"

    # Canonical key: sorted comma list
    key=$(echo "$shared" | tr ',' '\n' | sort -n | paste -sd, - | sed 's/,$//')

    if [[ -z "${l3_to_ccx[$key]}" ]]; then
      l3_to_ccx[$key]=$ccx_id
      CCX_CORES[$ccx_id]="$key"
      CCX_MAX[$ccx_id]=0
      ((ccx_id++))
    fi
    CPU_TO_CCX[$cpu]=${l3_to_ccx[$key]}
    CPU_MAX[$cpu]=0
  done
}

loggerCpuClock() {
  local n cpu khz ghz prev cc
  n=$(nproc 2>/dev/null || echo 1)

  # Load baselines from clocks.log (so external resets / Clears are picked up)
  if [[ -f "$CLOCKS_LOG" ]]; then
    while IFS='=' read -r k v || [[ -n "$k" ]]; do
      k=${k//[[:space:]]/}
      v=${v//[[:space:]]/}
      [[ -z "$k" || -z "$v" ]] && continue
      case "$k" in
        GLOBAL) ;;
        CPU[0-9]*)
          cpu=${k#CPU}
          CPU_MAX[$cpu]="$v"
          ;;
        CCX[0-9]*)
          # only plain CCX<N>, not the MAP lines
          if [[ "$k" != CCX_FOR* && "$k" != CORES_IN* ]]; then
            cc=${k#CCX}
            CCX_MAX[$cc]="$v"
          fi
          ;;
      esac
    done < "$CLOCKS_LOG"
  fi

  local live_global=0

  for ((cpu=0; cpu<n; cpu++)); do
    local f="/sys/devices/system/cpu/cpu${cpu}/cpufreq/scaling_cur_freq"
    [[ -r "$f" ]] || continue
    khz=$(<"$f")
    ghz=$(awk "BEGIN { printf \"%.3f\", $khz / 1000000 }")

    # Per-CPU max
    prev=${CPU_MAX[$cpu]:-0}
    if awk -v cur="$ghz" -v last="$prev" 'BEGIN { exit !(cur > last) }'; then
      CPU_MAX[$cpu]="$ghz"
    fi

    # Track live global for classic clock.log
    if awk -v cur="$ghz" -v last="$live_global" 'BEGIN { exit !(cur > last) }'; then
      live_global="$ghz"
    fi

    # Per-CCX max
    cc=${CPU_TO_CCX[$cpu]:-0}
    prev=${CCX_MAX[$cc]:-0}
    if awk -v cur="$ghz" -v last="$prev" 'BEGIN { exit !(cur > last) }'; then
      CCX_MAX[$cc]="$ghz"
    fi
  done

  # Always update the classic single-value file (compat with existing driver prints + old GUI)
  printf "%.3f\n" "$live_global" > "$CLOCK_LOG"

  # Write rich per-core / per-CCX data (source of truth for new UI)
  {
    printf "GLOBAL=%.3f\n" "$live_global"
    for ((cpu=0; cpu<n; cpu++)); do
      printf "CPU%d=%s\n" "$cpu" "${CPU_MAX[$cpu]:-0}"
    done
    for cc in "${!CCX_MAX[@]}"; do
      printf "CCX%d=%s\n" "$cc" "${CCX_MAX[$cc]}"
    done
    for ((cpu=0; cpu<n; cpu++)); do
      printf "CCX_FOR_CPU%d=%s\n" "$cpu" "${CPU_TO_CCX[$cpu]:-0}"
    done
    for cc in "${!CCX_CORES[@]}"; do
      printf "CORES_IN_CCX%d=%s\n" "$cc" "${CCX_CORES[$cc]}"
    done
  } > "$CLOCKS_LOG"
}

# Discover CCX topology once when the logger starts (before the sampling loop)
discover_topology

loggerErrorCheck() {
  LOG_PRIORITY="err"
  EXCLUDE=("libinput" "bluetooth" "cityfailed" "plasmashell" "mouse" "keyboard" "chrome" "firefox" "librewold" "floorp" "discord" "brave" "electron" "udev")

  ERRORS_HARDWARE=$(journalctl --since="@${START_TIME}" -p "$LOG_PRIORITY" -k --no-pager -q 2>/dev/null | grep -E 'MCE|Machine Check|Hardware Error|EDAC|ECC|NVRM|Xid|amdgpu|i915|GPU fault|GPU HANG')
  ERRORS_FLAG=$(journalctl --since="@${START_TIME}" -p "$LOG_PRIORITY" --no-pager -q 2>/dev/null)
  ERRORS_DUMPED=$(journalctl --since="@${START_TIME}" --no-pager | grep -i "dumped core")
  ERRORS_SEGFAULT=$(journalctl --since="@${START_TIME}" --no-pager | grep -i "segfault")

  COREDUMPS=$(coredumpctl --since "@${START_TIME}" list --no-pager 2>/dev/null | awk 'gsub(/ /,"")>=5')
  [ -z "$COREDUMPS" ] && COREDUMPS=""

  for word in "${EXCLUDE[@]}"; do
    ERRORS_HARDWARE=$(echo "$ERRORS_HARDWARE" | grep -vi "$word")
    ERRORS_FLAG=$(echo "$ERRORS_FLAG" | grep -vi "$word")
    ERRORS_DUMPED=$(echo "$ERRORS_DUMPED" | grep -vi "$word")
    ERRORS_SEGFAULT=$(echo "$ERRORS_SEGFAULT" | grep -vi "$word")
    COREDUMPS=$(echo "$COREDUMPS" | grep -vi "$word")
  done

  ERRORS_HARDWARE=$(echo "$ERRORS_HARDWARE" | awk 'gsub(/ /,"")>=5')
  ERRORS_FLAG=$(echo "$ERRORS_FLAG" | awk 'gsub(/ /,"")>=5')
  ERRORS_DUMPED=$(echo "$ERRORS_DUMPED" | awk 'gsub(/ /,"")>=5')
  ERRORS_SEGFAULT=$(echo "$ERRORS_SEGFAULT" | awk 'gsub(/ /,"")>=5')

  if [ -n "$ERRORS_DUMPED" ] || [ -n "$ERRORS_SEGFAULT" ] || [ -n "$COREDUMPS" ] || [ -n "$ERRORS_FLAG" ] || [ -n "$ERRORS_HARDWARE" ]; then
    {
      echo "=== Errors detected at $(date -Iseconds) ==="
      [ -n "$ERRORS_HARDWARE" ] && echo "$ERRORS_HARDWARE"
      [ -n "$ERRORS_FLAG" ] && echo "$ERRORS_FLAG"
      [ -n "$ERRORS_DUMPED" ] && echo "$ERRORS_DUMPED"
      [ -n "$ERRORS_SEGFAULT" ] && echo "$ERRORS_SEGFAULT"
      [ -n "$COREDUMPS" ] && echo "=== Coredumps ===" && echo "$COREDUMPS"
      echo "========================================"
    } > "$ERROR_LOG"
    exit 1
  fi
}

while :; do
  loggerCpuClock
  loggerErrorCheck
  sleep 2
done