"""로깅 설정 모듈"""

import logging
from pathlib import Path

try:
    from .config import get_config
except ImportError:
    from config import get_config  # type: ignore[reportMissingImports]


def setup_logger(name: str = "gwanbo_crawler") -> logging.Logger:
    """
    로거를 설정합니다.
    
    Args:
        name: 로거 이름
        
    Returns:
        설정된 로거
    """
    config = get_config()
    log_config = config.get_logging_config()
    
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, log_config.get('level', 'INFO')))
    
    # 로그 디렉토리 생성
    log_file = log_config.get('log_file', 'logs/crawler.log')
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    
    # 파일 핸들러
    file_handler = logging.FileHandler(log_file, encoding='utf-8')
    file_handler.setLevel(getattr(logging, log_config.get('level', 'INFO')))
    
    # 콘솔 핸들러
    console_handler = logging.StreamHandler()
    console_handler.setLevel(getattr(logging, log_config.get('level', 'INFO')))
    
    # 포맷터
    formatter = logging.Formatter(
        log_config.get('format', '%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    )
    file_handler.setFormatter(formatter)
    console_handler.setFormatter(formatter)
    
    # 핸들러 추가 (중복 방지)
    if not logger.handlers:
        logger.addHandler(file_handler)
        logger.addHandler(console_handler)
    
    return logger
