#!/usr/bin/env python3
"""HuggingFace 데이터셋 업로드 스크립트"""

import json
import sys
from pathlib import Path
from typing import Dict, List, Any

import click
from tqdm import tqdm

try:
    import datasets
except ImportError:
    print("Error: 'datasets' 패키지가 필요합니다. pip install datasets huggingface_hub")
    sys.exit(1)

# 부모 디렉토리를 sys.path에 추가
sys.path.insert(0, str(Path(__file__).parent.parent))
from src.config import get_config
from src.logger import setup_logger


logger = setup_logger(__name__)

# 카테고리 목록
CATEGORIES = [
    "감사원", "고시", "공고", "국가인권위원회", "국회", "기타",
    "대통령령", "대통령지시사항", "법률", "법원", "부령", "상훈",
    "선거관리위원회", "조달관보", "조약", "지방자치단체", "총리령",
    "헌법재판소", "훈령"
]

SOURCES = ["pety", "searchThema"]


class HFUploader:
    """HuggingFace 데이터셋 업로더"""

    def __init__(self, repo_id: str, data_dir: str | Path):
        """
        초기화

        Args:
            repo_id: HuggingFace 리포지터리 ID (e.g. "yakdoli/peti-dataset")
            data_dir: 메타데이터 디렉토리
        """
        self.repo_id = repo_id
        self.data_dir = Path(data_dir) if isinstance(data_dir, str) else data_dir
        self.config = get_config()
        self.batch_state_file = self.data_dir / "hf_batch_state.json"
        self.batch_state = self._load_batch_state()

    def _load_batch_state(self) -> Dict[str, int]:
        """업로드 상태 로드"""
        if self.batch_state_file.exists():
            try:
                with open(self.batch_state_file, 'r') as f:
                    state = json.load(f)
                logger.info(f"배치 상태 로드: {state['done']}/{state['total']}")
                return state
            except Exception as e:
                logger.warning(f"배치 상태 로드 오류: {e}")
                return {"done": 0, "total": 0}
        return {"done": 0, "total": 0}

    def _save_batch_state(self) -> None:
        """업로드 상태 저장"""
        self.batch_state_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.batch_state_file, 'w') as f:
            json.dump(self.batch_state, f)
        logger.info(f"배치 상태 저장: {self.batch_state['done']}/{self.batch_state['total']}")

    def load_metadata_items(self, source: str, category: str) -> List[Dict[str, Any]]:
        """
        메타데이터 아이템 로드

        Args:
            source: 'pety' 또는 'searchThema'
            category: 카테고리명 (e.g. '법률')

        Returns:
            메타데이터 아이템 리스트
        """
        if source == "pety":
            metadata_file = self.data_dir.parent / "metadata" / "metadata.json"
        else:  # searchThema
            metadata_file = self.data_dir / f"metadata_{category}.json"

        if not metadata_file.exists():
            logger.warning(f"메타데이터 파일 없음: {metadata_file}")
            return []

        try:
            with open(metadata_file, 'r', encoding='utf-8') as f:
                content = f.read().strip()
                if not content:
                    return []
                data = json.loads(content)
                # 배열 또는 딕셔너리 모두 지원
                if isinstance(data, dict):
                    return list(data.values())
                return data if isinstance(data, list) else []
        except Exception as e:
            logger.error(f"메타데이터 로드 오류 ({metadata_file}): {e}")
            return []

    def prepare_dataset_records(
        self, source: str, category: str, items: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        데이터셋 레코드 준비

        Args:
            source: 소스명
            category: 카테고리명
            items: 메타데이터 아이템 리스트

        Returns:
            HF 업로드용 레코드 리스트
        """
        records = []
        for item in items:
            try:
                record = {
                    "id": item.get("id") or item.get("stored_toc_seq"),
                    "source": source,
                    "category": category,
                    "title": item.get("title") or item.get("stored_field_subject"),
                    "date": item.get("date") or item.get("stored_field_month"),
                    "agency": item.get("agency") or item.get("stored_organ_nm"),
                    "pdf_status": item.get("pdf", {}).get("status", "unknown"),
                    "ocr_status": item.get("ocr", {}).get("status", "unknown"),
                }
                if record["id"]:  # ID가 있어야만 추가
                    records.append(record)
            except Exception as e:
                logger.debug(f"레코드 준비 오류: {e}")
                continue
        return records

    def upload_split(self, source: str, category: str, dry_run: bool = False) -> bool:
        """
        단일 split 업로드

        Args:
            source: 소스명
            category: 카테고리명
            dry_run: 드라이런 모드

        Returns:
            성공 여부
        """
        config_name = f"{source}/{category}"
        items = self.load_metadata_items(source, category)

        if not items:
            logger.warning(f"아이템 없음: {config_name}")
            return False

        records = self.prepare_dataset_records(source, category, items)
        logger.info(f"업로드 준비 ({config_name}): {len(records)}개 레코드")

        if dry_run:
            logger.info(f"[DRY RUN] {config_name} 업로드 스킵")
            return True

        try:
            dataset = datasets.Dataset.from_dict({
                key: [r[key] for r in records]
                for key in records[0].keys()
            })

            logger.info(f"HF 업로드 시작: {config_name} ({len(records)}개)")
            dataset.push_to_hub(
                repo_id=self.repo_id,
                config_name=config_name,
                split="main",
                private=False,
            )
            logger.info(f"HF 업로드 완료: {config_name}")
            return True
        except Exception as e:
            logger.error(f"HF 업로드 오류 ({config_name}): {e}")
            return False

    def upload_all(self, dry_run: bool = False) -> None:
        """모든 split 업로드"""
        total_splits = len(SOURCES) + (len(CATEGORIES) if "searchThema" in SOURCES else 0)
        uploaded = 0

        with tqdm(total=total_splits, desc="HF 업로드") as pbar:
            # pety 소스
            if "pety" in SOURCES:
                if self.upload_split("pety", "공직자재산공개", dry_run):
                    uploaded += 1
                pbar.update(1)

            # searchThema 소스
            if "searchThema" in SOURCES:
                for category in CATEGORIES:
                    if self.upload_split("searchThema", category, dry_run):
                        uploaded += 1
                    pbar.update(1)

        logger.info(f"업로드 완료: {uploaded}/{total_splits}개 split")


@click.command()
@click.option(
    '--repo-id',
    default='yakdoli/peti-dataset',
    help='HuggingFace 리포지터리 ID'
)
@click.option(
    '--data-dir',
    type=click.Path(exists=True),
    default='data/searchThema',
    help='메타데이터 디렉토리'
)
@click.option(
    '--dry-run',
    is_flag=True,
    help='드라이런 모드'
)
def main(repo_id: str, data_dir: str, dry_run: bool) -> None:
    """HuggingFace 데이터셋 업로드"""
    logger.info(f"HuggingFace 업로드 시작")
    logger.info(f"  리포지터리: {repo_id}")
    logger.info(f"  데이터 디렉토리: {data_dir}")
    logger.info(f"  드라이런: {dry_run}")

    uploader = HFUploader(repo_id, data_dir)
    uploader.upload_all(dry_run=dry_run)

    logger.info(f"HuggingFace 업로드 종료")


if __name__ == '__main__':
    main()
