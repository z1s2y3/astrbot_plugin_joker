import os
from pathlib import Path
from astrbot.api import logger
from astrbot.core import get_astrbot_data_path


def get_plugin_data_path():
    data_path = Path(get_astrbot_data_path()) / "plugin_data" / "qqadmin"
    data_path.mkdir(parents=True, exist_ok=True)
    return data_path


def get_groups_storage_dir():
    """获取群存储根目录"""
    storage_dir = get_plugin_data_path() / "group_storage"
    storage_dir.mkdir(parents=True, exist_ok=True)
    return storage_dir


def get_group_storage_dir(group_id: str):
    """
    获取指定群的存储目录
    
    Args:
        group_id: 群号
    
    Returns:
        Path: 该群的存储目录路径
    """
    group_dir = get_groups_storage_dir() / group_id
    group_dir.mkdir(parents=True, exist_ok=True)
    return group_dir


def get_group_subdir(group_id: str, subdir: str):
    """
    获取指定群的子目录
    
    Args:
        group_id: 群号
        subdir: 子目录名（例如 "images", "files", "videos"）
    
    Returns:
        Path: 该子目录的路径
    """
    group_dir = get_group_storage_dir(group_id)
    subdir_path = group_dir / subdir
    subdir_path.mkdir(parents=True, exist_ok=True)
    return subdir_path


def save_group_file(group_id: str, filename: str, content: bytes, subdir: str = "files"):
    """
    保存文件到群存储目录
    
    Args:
        group_id: 群号
        filename: 文件名
        content: 文件内容（bytes）
        subdir: 子目录名（默认为 "files"）
    
    Returns:
        Path: 保存的文件路径
    """
    file_dir = get_group_subdir(group_id, subdir)
    file_path = file_dir / filename
    
    with open(file_path, 'wb') as f:
        f.write(content)
    
    logger.info(f"文件已保存到群 {group_id}: {subdir}/{filename}")
    return file_path


def load_group_file(group_id: str, filename: str, subdir: str = "files"):
    """
    从群存储目录加载文件
    
    Args:
        group_id: 群号
        filename: 文件名
        subdir: 子目录名（默认为 "files"）
    
    Returns:
        bytes: 文件内容，如果文件不存在则返回 None
    """
    file_dir = get_group_subdir(group_id, subdir)
    file_path = file_dir / filename
    
    if not file_path.exists():
        logger.warning(f"文件不存在: {group_id}/{subdir}/{filename}")
        return None
    
    with open(file_path, 'rb') as f:
        return f.read()


def list_group_files(group_id: str, subdir: str = "files"):
    """
    列出群存储目录中的所有文件
    
    Args:
        group_id: 群号
        subdir: 子目录名（默认为 "files"）
    
    Returns:
        list: 文件列表
    """
    file_dir = get_group_subdir(group_id, subdir)
    
    if not file_dir.exists():
        return []
    
    files = []
    for item in file_dir.iterdir():
        if item.is_file():
            files.append(item.name)
    
    return sorted(files)


def delete_group_file(group_id: str, filename: str, subdir: str = "files"):
    """
    删除群存储目录中的文件
    
    Args:
        group_id: 群号
        filename: 文件名
        subdir: 子目录名（默认为 "files"）
    
    Returns:
        bool: 删除是否成功
    """
    file_dir = get_group_subdir(group_id, subdir)
    file_path = file_dir / filename
    
    if not file_path.exists():
        logger.warning(f"文件不存在，无法删除: {group_id}/{subdir}/{filename}")
        return False
    
    try:
        os.remove(file_path)
        logger.info(f"文件已删除: {group_id}/{subdir}/{filename}")
        return True
    except Exception as e:
        logger.error(f"删除文件失败: {group_id}/{subdir}/{filename}, 错误: {e}")
        return False


def get_group_file_path(group_id: str, filename: str, subdir: str = "files"):
    """
    获取群存储目录中文件的完整路径
    
    Args:
        group_id: 群号
        filename: 文件名
        subdir: 子目录名（默认为 "files"）
    
    Returns:
        Path: 文件路径
    """
    file_dir = get_group_subdir(group_id, subdir)
    return file_dir / filename


def initialize_group_storage(group_id: str):
    """
    初始化群存储目录，创建常用的子目录
    
    Args:
        group_id: 群号
    """
    # 创建常用的子目录
    common_subdirs = ["images", "files", "videos", "audios", "logs"]
    for subdir in common_subdirs:
        get_group_subdir(group_id, subdir)
    
    logger.info(f"群存储目录已初始化: {group_id}")
