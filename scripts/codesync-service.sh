#!/usr/bin/env bash
set -Eeuo pipefail

# Manage a CodeSync virtual environment and its systemd service.

SERVICE_NAME="${CODESYNC_SERVICE_NAME:-codesync}"
SERVICE_PORT="${CODESYNC_PORT:-7788}"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"
VENV_DIR="${PROJECT_DIR}/.venv"
UNIT_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

if [[ -t 1 ]]; then
  readonly C_RESET=$'\033[0m'
  readonly C_DIM=$'\033[2m'
  readonly C_BOLD=$'\033[1m'
  readonly C_CYAN=$'\033[36m'
  readonly C_BLUE=$'\033[34m'
  readonly C_GREEN=$'\033[32m'
  readonly C_YELLOW=$'\033[33m'
  readonly C_RED=$'\033[31m'
else
  readonly C_RESET='' C_DIM='' C_BOLD='' C_CYAN='' C_BLUE='' C_GREEN='' C_YELLOW='' C_RED=''
fi

log() {
  printf '[codesync] %s\n' "$*"
}

die() {
  printf '[codesync] 错误：%s\n' "$*" >&2
  exit 1
}

usage() {
  cat <<EOF
用法：$(basename "$0") <命令>

命令：
  install      创建 .venv、安装依赖并注册/启动 systemd 服务
  status       查看服务状态和最近日志
  restart      重启服务
  stop         停止服务
  uninstall    停止并卸载 systemd 服务（保留 .venv 和配置）
  help         显示帮助

可选环境变量：
  CODESYNC_SERVICE_NAME  服务名，默认为 codesync
  CODESYNC_PORT          Web 端口，默认为 7788

不带命令且在终端中运行时，会进入交互式 TUI 菜单。
EOF
}

clear_screen() {
  printf '\033[2J\033[H'
}

service_state() {
  local state configured_port
  if ! run_root systemctl cat "${SERVICE_NAME}.service" >/dev/null 2>&1; then
    printf 'not-installed'
    return
  fi
  if [[ -r "${UNIT_FILE}" ]]; then
    configured_port="$(sed -n 's/^Environment=CODESYNC_PORT=//p' "${UNIT_FILE}" | head -n 1)"
    if [[ "${configured_port}" =~ ^[0-9]+$ ]] && ((10#${configured_port} >= 1 && 10#${configured_port} <= 65535)); then
      SERVICE_PORT="${configured_port}"
    fi
  fi
  state="$(run_root systemctl is-active "${SERVICE_NAME}.service" 2>/dev/null || true)"
  [[ -n "${state}" ]] || state="not-installed"
  printf '%s' "${state}"
}

state_color() {
  case "$1" in
    active) printf '%s' "${C_GREEN}" ;;
    activating|deactivating) printf '%s' "${C_YELLOW}" ;;
    failed) printf '%s' "${C_RED}" ;;
    *) printf '%s' "${C_DIM}" ;;
  esac
}

state_label() {
  case "$1" in
    active) printf '运行中' ;;
    inactive) printf '已停止' ;;
    failed) printf '失败' ;;
    activating) printf '启动中' ;;
    deactivating) printf '停止中' ;;
    not-installed) printf '未安装' ;;
    *) printf '%s' "$1" ;;
  esac
}

pause_menu() {
  printf '\n%s按 Enter 返回菜单%s' "${C_DIM}" "${C_RESET}"
  read -r
}

confirm_action() {
  local answer
  printf '%s确认执行？[y/N] %s' "${C_YELLOW}" "${C_RESET}"
  read -r answer
  [[ "${answer}" =~ ^[Yy]$ ]]
}

validate_port() {
  [[ "$1" =~ ^[0-9]+$ ]] && ((10#$1 >= 1 && 10#$1 <= 65535)) \
    || die "端口必须是 1-65535 之间的整数"
}

prompt_port() {
  local input
  printf '%s服务端口 [%s]：%s' "${C_CYAN}" "${SERVICE_PORT}" "${C_RESET}"
  read -r input
  if [[ -n "${input}" ]]; then
    if ! [[ "${input}" =~ ^[0-9]+$ ]] || ((10#${input} < 1 || 10#${input} > 65535)); then
      log '端口必须是 1-65535 之间的整数'
      return 1
    fi
    SERVICE_PORT="${input}"
  fi
}

draw_menu() {
  local selected="$1" state venv_label color
  state="$(service_state)"
  color="$(state_color "${state}")"
  if [[ -x "${VENV_DIR}/bin/python" ]]; then
    venv_label="${C_GREEN}已创建${C_RESET}"
  else
    venv_label="${C_YELLOW}未创建${C_RESET}"
  fi

  clear_screen
  printf '%s╭────────────────────────────────────────────────────────────╮%s\n' "${C_CYAN}" "${C_RESET}"
  printf '%s│%s  %sCodeSync Service Console%s                            %s│%s\n' "${C_CYAN}" "${C_RESET}" "${C_BOLD}" "${C_RESET}" "${C_CYAN}" "${C_RESET}"
  printf '%s├────────────────────────────────────────────────────────────┤%s\n' "${C_CYAN}" "${C_RESET}"
  printf '%s│%s  服务名  %-18s 状态  %s%-10s%s            %s│%s\n' "${C_CYAN}" "${C_RESET}" "${SERVICE_NAME}" "${color}" "$(state_label "${state}")" "${C_RESET}" "${C_CYAN}" "${C_RESET}"
  printf '%s│%s  地址    %-18s 虚拟环境  %-10b %s│%s\n' "${C_CYAN}" "${C_RESET}" "http://localhost:${SERVICE_PORT}" "${venv_label}" "${C_CYAN}" "${C_RESET}"
  printf '%s╰────────────────────────────────────────────────────────────╯%s\n\n' "${C_CYAN}" "${C_RESET}"

  local -a labels=(
    '安装 / 更新环境并启动服务'
    '查看服务状态和最近日志'
    '重启服务'
    '停止服务'
    '卸载 systemd 服务'
    '刷新状态'
  )
  local i prefix
  for ((i = 0; i < ${#labels[@]}; i++)); do
    if [[ "${i}" -eq "${selected}" ]]; then
      prefix="${C_BLUE}${C_BOLD}❯${C_RESET}"
      printf '  %s %s%s%s\n' "${prefix}" "$((i + 1))" "${C_BOLD}" "${labels[$i]}${C_RESET}"
    else
      printf '    %s %s\n' "$((i + 1))" "${labels[$i]}"
    fi
  done
  printf '\n%s  ↑/↓ 或 j/k 选择  ·  Enter 执行  ·  q 退出%s\n' "${C_DIM}" "${C_RESET}"
}

interactive_menu() {
  local selected=0 key action_count=6
  while true; do
    draw_menu "${selected}"
    IFS= read -r -s -n 1 key
    case "${key}" in
      q|Q) clear_screen; log '已退出'; return 0 ;;
      '')
        clear_screen
        case "${selected}" in
          0) prompt_port && ensure_venv && install_service || true ;;
          1) status_service ;;
          2) restart_service || true ;;
          3) stop_service || true ;;
          4)
            if confirm_action; then
              uninstall_service
            else
              log '已取消卸载'
            fi
            ;;
          5) continue ;;
        esac
        pause_menu
        ;;
      $'\033')
        IFS= read -r -s -n 2 key
        case "${key}" in
          '[A') selected=$(( (selected - 1 + action_count) % action_count )) ;;
          '[B') selected=$(( (selected + 1) % action_count )) ;;
        esac
        ;;
      j|J) selected=$(( (selected + 1) % action_count )) ;;
      k|K) selected=$(( (selected - 1 + action_count) % action_count )) ;;
      [1-6]) selected=$((10#${key} - 1)) ;;
    esac
  done
}

run_root() {
  if [[ "${EUID}" -eq 0 ]]; then
    "$@"
  else
    sudo "$@"
  fi
}

require_commands() {
  local command_name
  for command_name in "$@"; do
    command -v "${command_name}" >/dev/null 2>&1 || die "缺少命令：${command_name}"
  done
}

ensure_sshpass() {
  if command -v sshpass >/dev/null 2>&1; then
    return
  fi

  log '未检测到 sshpass，正在安装密码认证所需依赖'
  if command -v apt-get >/dev/null 2>&1; then
    run_root apt-get update
    run_root apt-get install -y sshpass
  elif command -v dnf >/dev/null 2>&1; then
    run_root dnf install -y sshpass
  elif command -v yum >/dev/null 2>&1; then
    run_root yum install -y sshpass
  elif command -v pacman >/dev/null 2>&1; then
    run_root pacman -S --needed --noconfirm sshpass
  else
    die '无法自动安装 sshpass，请使用系统包管理器安装后重试'
  fi
  require_commands sshpass
}

require_privileges() {
  if [[ "${EUID}" -ne 0 ]]; then
    require_commands sudo
  fi
}

validate_service_name() {
  [[ "${SERVICE_NAME}" =~ ^[A-Za-z0-9_.@-]+$ ]] \
    || die "服务名只能包含字母、数字、下划线、点、@ 和连字符"
}

ensure_venv() {
  require_commands python3

  if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
    log "创建虚拟环境：${VENV_DIR}"
    python3 -m venv "${VENV_DIR}" \
      || die "创建 .venv 失败，请确认已安装 python3-venv"
  fi

  log "安装 Python 依赖"
  "${VENV_DIR}/bin/python" -m pip install -r "${PROJECT_DIR}/requirements.txt"
}

write_unit_file() {
  local service_user service_group user_home unit_tmp
  service_user="${CODESYNC_USER:-${SUDO_USER:-$(id -un)}}"
  service_group="${CODESYNC_GROUP:-$(id -gn "${service_user}")}"
  user_home="$(getent passwd "${service_user}" | cut -d: -f6)"
  [[ -n "${user_home}" && -d "${user_home}" ]] || die "无法确定用户 ${service_user} 的 HOME 目录"

  unit_tmp="$(mktemp)"
  trap 'rm -f -- "${unit_tmp}"' RETURN
  cat >"${unit_tmp}" <<EOF
[Unit]
Description=CodeSync rsync web manager
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${service_user}
Group=${service_group}
WorkingDirectory=${PROJECT_DIR}
Environment=HOME=${user_home}
Environment=PYTHONUNBUFFERED=1
Environment=CODESYNC_PORT=${SERVICE_PORT}
ExecStart=${VENV_DIR}/bin/python ${PROJECT_DIR}/app.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

  run_root install -o root -g root -m 0644 "${unit_tmp}" "${UNIT_FILE}"
  trap - RETURN
  rm -f -- "${unit_tmp}"
}

install_service() {
  [[ "$(uname -s)" == "Linux" ]] || die "当前脚本仅支持 Linux systemd"
  require_privileges
  require_commands systemctl getent rsync
  ensure_sshpass
  validate_port "${SERVICE_PORT}"
  write_unit_file

  log "注册并启动服务：${SERVICE_NAME}"
  run_root systemctl daemon-reload
  if ! run_root systemctl enable --now "${SERVICE_NAME}.service"; then
    log "服务启动失败，请选择“查看服务状态和最近日志”获取详情"
    return 1
  fi
  log "服务已启动：http://localhost:${SERVICE_PORT}"
}

status_service() {
  [[ "$(uname -s)" == "Linux" ]] || die "当前脚本仅支持 Linux systemd"
  require_privileges
  require_commands systemctl
  run_root systemctl status --no-pager "${SERVICE_NAME}.service" || true
  printf '\n'
  run_root journalctl -u "${SERVICE_NAME}.service" -n 30 --no-pager || true
}

restart_service() {
  require_privileges
  require_commands systemctl
  if ! run_root systemctl restart "${SERVICE_NAME}.service"; then
    log "服务重启失败，请先安装服务或查看服务状态"
    return 1
  fi
  log "服务已重启"
}

stop_service() {
  require_privileges
  require_commands systemctl
  if ! run_root systemctl stop "${SERVICE_NAME}.service"; then
    log "服务停止失败，可能尚未安装"
    return 1
  fi
  log "服务已停止"
}

uninstall_service() {
  [[ "$(uname -s)" == "Linux" ]] || die "当前脚本仅支持 Linux systemd"
  require_privileges
  require_commands systemctl

  log "卸载服务：${SERVICE_NAME}"
  run_root systemctl disable --now "${SERVICE_NAME}.service" 2>/dev/null || true
  run_root rm -f -- "${UNIT_FILE}"
  run_root systemctl daemon-reload
  run_root systemctl reset-failed "${SERVICE_NAME}.service" 2>/dev/null || true
  log "服务已卸载；.venv 和 ~/.codesync 配置已保留"
}

main() {
  local command_name="${1:-help}"
  validate_service_name
  if [[ "$#" -eq 0 && -t 0 && -t 1 ]]; then
    interactive_menu
    return
  fi
  case "${command_name}" in
    install)
      validate_port "${SERVICE_PORT}"
      if [[ "${EUID}" -eq 0 && -n "${SUDO_USER:-}" && -z "${CODESYNC_USER:-}" ]]; then
        die "请不要使用 sudo 直接执行 install；脚本会自行调用 sudo，以便由当前用户创建 .venv"
      fi
      ensure_venv
      install_service
      ;;
    status) status_service ;;
    restart) restart_service ;;
    stop) stop_service ;;
    uninstall) uninstall_service ;;
    help|-h|--help) usage ;;
    *) usage >&2; exit 2 ;;
  esac
}

main "$@"
