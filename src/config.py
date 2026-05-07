from typing import Optional
"""설정 관리 모듈"""

from typing import Dict, Any
from pathlib import Path
from datetime import datetime
import yaml
import os

class Config:
    """크롤러 설정 클래스"""

    def __init__(self, config_file: str = "config/config.yaml"):
        """
        설정 파일을 로드합니다.
        
        Args:
            config_file: YAML 설정 파일 경로
        """
        self.config_file = config_file
        self.config: Dict[str, Any] = {}
        self.load_config()
        self._ensure_directories()

    def load_config(self) -> None:
        """YAML 설정 파일을 로드합니다."""
        if not os.path.exists(self.config_file):
            raise FileNotFoundError(f"설정 파일을 찾을 수 없습니다: {self.config_file}")
        
        with open(self.config_file, 'r', encoding='utf-8') as f:
            self.config = yaml.safe_load(f)

    def _ensure_directories(self) -> None:
        """필요한 디렉토리를 생성합니다."""
        download_config = self.config.get('download', {})
        
        directories = [
            download_config.get('pdf_directory', 'data/pdfs'),
            download_config.get('metadata_directory', 'data/metadata'),
            download_config.get('ocr_ready_directory', 'data/ocr_ready'),
            self.config.get('logging', {}).get('log_file', 'logs/crawler.log').rsplit('/', 1)[0],
        ]
        
        for directory in directories:
            Path(directory).mkdir(parents=True, exist_ok=True)

    def get(self, key: str, default: Any = None) -> Any:
        """
        설정 값을 가져옵니다.
        
        Args:
            key: 설정 키 (점으로 구분, 예: 'crawler.start_date')
            default: 기본값
            
        Returns:
            설정 값 또는 기본값
        """
        keys = key.split('.')
        value = self.config
        
        for k in keys:
            if isinstance(value, dict):
                value = value.get(k, default)
            else:
                return default
        
        return value

    def get_crawler_config(self) -> Dict[str, Any]:
        """크롤러 설정을 반환합니다."""
        return self.config.get('crawler', {})

    def get_download_config(self) -> Dict[str, Any]:
        """다운로드 설정을 반환합니다."""
        return self.config.get('download', {})

    def get_metadata_config(self) -> Dict[str, Any]:
        """메타데이터 설정을 반환합니다."""
        return self.config.get('metadata', {})

    def get_logging_config(self) -> Dict[str, Any]:
        """로깅 설정을 반환합니다."""
        return self.config.get('logging', {})

    def get_ocr_config(self) -> Dict[str, Any]:
        """OCR 설정을 반환합니다."""
        return self.config.get('ocr', {})


# 글로벌 설정 인스턴스
_config: Optional[Config] = None


def get_config() -> Config:
    """글로벌 설정 인스턴스를 가져옵니다."""
    global _config
    if _config is None:
        _config = Config()
    return _config
