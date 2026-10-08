# -*- coding: utf-8 -*-
"""
V10.10.16 跨进程守护线程单实例锁
Linux 使用 fcntl.flock 非阻塞文件锁确保多 Worker 环境下守护线程仅一份执行业务；
Windows/无 fcntl 环境直通（传统版单进程场景天然安全，行为不变）。
锁在进程存活期间持久持有，进程退出（含 Gunicorn max_requests 回收）后由 OS 自动释放。
"""

import os

try:
    import fcntl
    _HAS_FCNTL = True
except ImportError:
    _HAS_FCNTL = False

# 进程内已持有的锁：name -> file descriptor（防止重复获取）
_lock_holders = {}


def _get_lock_path(name):
    """获取锁文件路径，优先使用 data 目录（Docker 卷挂载点）"""
    base = os.path.dirname(os.path.abspath(__file__))
    data_dir = os.path.join(base, 'data')
    if not os.path.isdir(data_dir):
        data_dir = base
    return os.path.join(data_dir, f'.{name}.daemon.lock')


def try_acquire_daemon_lock(name):
    """
    尝试获取跨进程持久文件锁。
    成功（含本进程已持有）返回 True；已被其他进程持有返回 False。
    Windows/无 fcntl 环境始终返回 True（直通模式）。
    """
    if name in _lock_holders:
        return True
    if not _HAS_FCNTL:
        _lock_holders[name] = True
        return True
    try:
        lock_path = _get_lock_path(name)
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        fd = open(lock_path, 'w')
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_holders[name] = fd
        return True
    except (OSError, IOError):
        return False
