"""메타데이터 관리 모듈"""

import json
from pathlib import Path
from typing import Dict, List, Any, Optional
from datetime import datetime
import pandas as pd
import re

try:
    from .config import get_config
    from .logger import setup_logger
except ImportError:
    from config import get_config  # type: ignore[reportMissingImports]
    from logger import setup_logger  # type: ignore[reportMissingImports]


class MetadataManager:
    """메타데이터 관리 클래스"""

    def __init__(self):
        """메타데이터 매니저를 초기화합니다."""
        self.config = get_config()
        self.logger = setup_logger(__name__)
        self.metadata_dir = Path(self.config.get_download_config().get('metadata_directory', 'artifacts/metadata'))
        self.items_dir = self.metadata_dir / 'items'
        self.metadata_dir.mkdir(parents=True, exist_ok=True)
        self.items_dir.mkdir(parents=True, exist_ok=True)
        
        self.items: Dict[str, Dict[str, Any]] = {}
        self.load_existing_metadata()

    def load_existing_metadata(self) -> None:
        """기존 메타데이터를 로드합니다."""
        metadata_file = self.metadata_dir / 'metadata.json'
        if metadata_file.exists():
            try:
                with open(metadata_file, 'r', encoding='utf-8') as f:
                    self.items = json.load(f)
                self.logger.info(f"기존 메타데이터 로드: {len(self.items)}개")
            except Exception as e:
                self.logger.error(f"메타데이터 로드 오류: {e}")
        
        loaded_item_files = 0
        for item_file in self.items_dir.glob('*/*/*.json'):
            try:
                with open(item_file, 'r', encoding='utf-8') as f:
                    item = json.load(f)
                item_id = item.get('id')
                if item_id:
                    self.items[str(item_id)] = item
                    loaded_item_files += 1
            except Exception as e:
                self.logger.warning(f"항목 메타데이터 로드 오류 ({item_file}): {e}")
        
        if loaded_item_files:
            self.logger.info(f"항목별 메타데이터 로드: {loaded_item_files}개")

    def add_item(self, item: Dict[str, Any]) -> None:
        """
        메타데이터 항목을 추가합니다.
        
        Args:
            item: 메타데이터 항목
        """
        item_id = item.get('id')
        if item_id:
            self.items[str(item_id)] = item
            self.logger.debug(f"메타데이터 추가: {item_id}")

    def update_item(self, item_id: str, updated_data: Dict[str, Any]) -> None:
        """
        메타데이터 항목을 업데이트합니다.
        
        Args:
            item_id: 항목 ID
            updated_data: 업데이트 데이터
        """
        if str(item_id) in self.items:
            self.items[str(item_id)].update(updated_data)
            self.logger.debug(f"메타데이터 업데이트: {item_id}")
        else:
            self.items[str(item_id)] = updated_data
            self.logger.debug(f"메타데이터 신규 업데이트: {item_id}")

    def get_item(self, item_id: str) -> Optional[Dict[str, Any]]:
        """
        메타데이터 항목을 조회합니다.
        
        Args:
            item_id: 항목 ID
            
        Returns:
            메타데이터 항목 또는 None
        """
        return self.items.get(str(item_id))

    def get_all_items(self) -> List[Dict[str, Any]]:
        """
        모든 메타데이터 항목을 반환합니다.
        
        Returns:
            메타데이터 항목 리스트
        """
        return list(self.items.values())

    def get_items_by_status(self, status: str) -> List[Dict[str, Any]]:
        """
        상태별 메타데이터 항목을 반환합니다.
        
        Args:
            status: 상태 (pending, completed, failed 등)
            
        Returns:
            메타데이터 항목 리스트
        """
        return [item for item in self.items.values() if item.get('status') == status]

    def save_metadata(self) -> None:
        """메타데이터를 파일에 저장합니다."""
        metadata_file = self.metadata_dir / 'metadata.json'
        try:
            temp_file = metadata_file.with_suffix('.json.tmp')
            with open(temp_file, 'w', encoding='utf-8') as f:
                json.dump(self.items, f, ensure_ascii=False, indent=2, default=str)
            temp_file.replace(metadata_file)
            self.logger.info(f"메타데이터 저장: {metadata_file} ({len(self.items)}개)")
        except Exception as e:
            self.logger.error(f"메타데이터 저장 오류: {e}")

    def save_item(self, item: Dict[str, Any]) -> Path:
        """항목별 JSON 메타데이터를 저장합니다."""
        item_id = str(item.get('id', '')).strip()
        if not item_id:
            raise ValueError("항목 ID가 없습니다.")
        
        item_path = self.get_item_path(item)
        item_path.parent.mkdir(parents=True, exist_ok=True)
        temp_file = item_path.with_suffix('.json.tmp')
        with open(temp_file, 'w', encoding='utf-8') as f:
            json.dump(item, f, ensure_ascii=False, indent=2, default=str)
        temp_file.replace(item_path)
        self.add_item(item)
        return item_path

    def get_item_path(self, item: Dict[str, Any]) -> Path:
        """항목별 JSON 저장 경로를 반환합니다."""
        date_text = str(item.get('date') or 'unknown-date')
        year = date_text[:4] if re.match(r'^\d{4}', date_text) else 'unknown'
        date_key = date_text.replace('-', '') if re.match(r'^\d{4}-\d{2}-\d{2}$', date_text) else 'unknown'
        item_id = self._safe_filename(str(item.get('id')))
        return self.items_dir / year / date_key / f'{item_id}.json'

    def save_as_csv(self) -> None:
        """메타데이터를 CSV로 저장합니다."""
        try:
            df = pd.DataFrame([self._flatten_item(item) for item in self.items.values()])
            csv_file = self.metadata_dir / 'metadata.csv'
            df.to_csv(csv_file, index=False, encoding='utf-8-sig')
            self.logger.info(f"CSV 저장: {csv_file}")
        except Exception as e:
            self.logger.error(f"CSV 저장 오류: {e}")

    def save_by_category(self) -> None:
        """카테고리별로 메타데이터를 저장합니다."""
        categories = {}
        for item in self.items.values():
            category = item.get('category', 'unknown')
            if category not in categories:
                categories[category] = []
            categories[category].append(item)
        
        for old_file in self.metadata_dir.glob('metadata_*.json'):
            old_file.unlink(missing_ok=True)
        
        for category, items in categories.items():
            category_file = self.metadata_dir / f"metadata_{self._safe_filename(str(category))}.json"
            try:
                temp_file = category_file.with_suffix('.json.tmp')
                with open(temp_file, 'w', encoding='utf-8') as f:
                    json.dump(items, f, ensure_ascii=False, indent=2, default=str)
                temp_file.replace(category_file)
                self.logger.info(f"카테고리별 메타데이터 저장: {category_file} ({len(items)}개)")
            except Exception as e:
                self.logger.error(f"카테고리별 저장 오류: {e}")

    def get_statistics(self) -> Dict[str, Any]:
        """메타데이터 통계를 반환합니다."""
        statuses = {}
        categories = {}
        
        for item in self.items.values():
            status = item.get('status', 'unknown')
            statuses[status] = statuses.get(status, 0) + 1
            
            category = item.get('category', 'unknown')
            categories[category] = categories.get(category, 0) + 1
        
        return {
            'total_items': len(self.items),
            'statuses': statuses,
            'categories': categories,
            'save_date': datetime.now().isoformat(),
        }

    def rebuild_indexes(self) -> None:
        """항목별 JSON에서 aggregate JSON/CSV/category 인덱스를 재생성합니다."""
        self.items = {}
        for item_file in self.items_dir.glob('*/*/*.json'):
            try:
                with open(item_file, 'r', encoding='utf-8') as f:
                    item = json.load(f)
                item_id = item.get('id')
                if item_id:
                    self.items[str(item_id)] = item
            except Exception as e:
                self.logger.warning(f"인덱스 재생성 로드 오류 ({item_file}): {e}")
        self.save_metadata()
        self.save_as_csv()
        self.save_by_category()

    def _flatten_item(self, item: Dict[str, Any]) -> Dict[str, Any]:
        flat = dict(item)
        pdf = flat.pop('pdf', {}) or {}
        ocr = flat.pop('ocr', {}) or {}
        for key, value in pdf.items():
            flat[f'pdf_{key}'] = value
        for key, value in ocr.items():
            if key == 'extracted_metadata':
                flat['ocr_extracted_metadata'] = json.dumps(value, ensure_ascii=False, default=str)
            else:
                flat[f'ocr_{key}'] = value
        return flat

    @staticmethod
    def _safe_filename(value: str) -> str:
        return re.sub(r'[^0-9A-Za-z가-힣_.-]+', '_', value).strip('._') or 'unknown'
