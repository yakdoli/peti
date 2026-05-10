#!/usr/bin/env python3
"""HuggingFace 데이터셋 업로드 스크립트"""

import json
import os
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

try:
    from huggingface_hub import HfApi
except ImportError:
    HfApi = None

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
        self.hf_token = self._resolve_hf_token()
        self._maybe_disable_xet()

    @staticmethod
    def _resolve_hf_token() -> str | None:
        """환경변수에서 HF 토큰을 조회."""
        return (
            os.getenv("HF_TOKEN")
            or os.getenv("HUGGINGFACE_HUB_TOKEN")
            or os.getenv("HUGGINGFACE_TOKEN")
        )

    @staticmethod
    def _maybe_disable_xet() -> None:
        """컨테이너/프록시 환경에서 Xet CAS 경로를 우회하기 위해 기본 비활성화."""
        os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

    def ensure_repo(self, private: bool = False, exist_ok: bool = True, dry_run: bool = False) -> bool:
        """HF dataset repository 생성/검증."""
        if dry_run:
            logger.info(f"[DRY RUN] HF 리포지터리 생성 스킵: {self.repo_id}")
            return True
        if HfApi is None:
            logger.error("huggingface_hub 패키지가 없어 리포지터리 생성 불가")
            return False
        if not self.hf_token:
            logger.error("HF_TOKEN 환경변수가 없어 리포지터리 생성 불가")
            return False
        try:
            api = HfApi(token=self.hf_token)
            api.create_repo(
                repo_id=self.repo_id,
                repo_type="dataset",
                private=private,
                exist_ok=exist_ok,
                token=self.hf_token,
            )
            logger.info(f"HF dataset 리포지터리 준비 완료: {self.repo_id}")
            return True
        except Exception as e:
            logger.error(f"HF 리포지터리 생성 실패 ({self.repo_id}): {e}")
            return False

    def ensure_bucket(self, bucket_id: str, private: bool = False, exist_ok: bool = True, dry_run: bool = False) -> bool:
        """HF storage bucket 생성/검증."""
        if dry_run:
            logger.info(f"[DRY RUN] HF bucket 생성 스킵: {bucket_id}")
            return True
        if HfApi is None:
            logger.error("huggingface_hub 패키지가 없어 bucket 생성 불가")
            return False
        if not self.hf_token:
            logger.error("HF_TOKEN 환경변수가 없어 bucket 생성 불가")
            return False
        try:
            api = HfApi(token=self.hf_token)
            api.create_bucket(
                bucket_id=bucket_id,
                private=private,
                exist_ok=exist_ok,
                token=self.hf_token,
            )
            logger.info(f"HF bucket 준비 완료: {bucket_id}")
            return True
        except Exception as e:
            logger.error(f"HF bucket 생성 실패 ({bucket_id}): {e}")
            return False

    def sync_artifacts_to_bucket(
        self,
        bucket_id: str,
        source_dir: str | Path,
        bucket_prefix: str = "searchThema",
        dry_run: bool = False,
        allow_repo_fallback: bool = True,
    ) -> bool:
        """로컬 아티팩트를 HF bucket으로 동기화."""
        if HfApi is None:
            logger.error("huggingface_hub 패키지가 없어 bucket sync 불가")
            return False
        if not self.hf_token:
            logger.error("HF_TOKEN 환경변수가 없어 bucket sync 불가")
            return False
        source_dir = str(source_dir)
        dest = f"hf://buckets/{bucket_id}/{bucket_prefix}".rstrip("/")
        try:
            api = HfApi(token=self.hf_token)
            plan = api.sync_bucket(source=source_dir, dest=dest, token=self.hf_token, dry_run=True)
            logger.info(f"Bucket sync dry-run: uploads={len([op for op in plan.operations if op.action == 'upload'])}")
            if dry_run:
                logger.info("[DRY RUN] bucket sync apply 스킵")
                return True
            api.sync_bucket(source=source_dir, dest=dest, token=self.hf_token)
            logger.info(f"Bucket sync 완료: {source_dir} -> {dest}")
            return True
        except Exception as e:
            logger.error(f"Bucket sync 실패 ({source_dir} -> {dest}): {e}")
            if not allow_repo_fallback:
                return False
            logger.warning("Bucket sync 실패로 repo upload_folder fallback 시도")
            return self.upload_artifacts_via_repo(source_dir=source_dir, path_in_repo=bucket_prefix, dry_run=dry_run)

    def upload_artifacts_via_repo(self, source_dir: str | Path, path_in_repo: str = "searchThema", dry_run: bool = False) -> bool:
        """bucket sync 실패 시 dataset repo 업로드 fallback."""
        if HfApi is None:
            logger.error("huggingface_hub 패키지가 없어 repo fallback 불가")
            return False
        if not self.hf_token:
            logger.error("HF_TOKEN 환경변수가 없어 repo fallback 불가")
            return False
        if dry_run:
            logger.info(f"[DRY RUN] repo upload fallback 스킵: {self.repo_id} <- {source_dir}")
            return True
        try:
            api = HfApi(token=self.hf_token)
            api.create_repo(repo_id=self.repo_id, repo_type="dataset", private=False, exist_ok=True, token=self.hf_token)
            commit_info = api.upload_folder(
                repo_id=self.repo_id,
                repo_type="dataset",
                folder_path=str(source_dir),
                path_in_repo=path_in_repo,
                token=self.hf_token,
                commit_message=f"fallback upload: {path_in_repo}",
            )
            logger.info(f"Repo fallback 업로드 완료: {commit_info.commit_url}")
            return True
        except Exception as e:
            logger.error(f"Repo fallback 업로드 실패: {e}")
            logger.warning("Repo fallback 2차 시도: metadata만 업로드")
            try:
                commit_info = api.upload_folder(
                    repo_id=self.repo_id,
                    repo_type="dataset",
                    folder_path=str(source_dir),
                    path_in_repo=path_in_repo,
                    token=self.hf_token,
                    allow_patterns=["metadata/**"],
                    commit_message=f"fallback metadata-only: {path_in_repo}",
                )
                logger.info(f"Repo metadata-only 업로드 완료: {commit_info.commit_url}")
                logger.warning("PDF 업로드는 실패했지만 metadata 업로드는 완료되었습니다.")
                return True
            except Exception as metadata_error:
                logger.error(f"Repo metadata-only 업로드도 실패: {metadata_error}")
                return False

    def validate_artifacts(self) -> Dict[str, int]:
        """업로드 대상 아티팩트 존재 여부 점검."""
        summary = {"found_files": 0, "found_items": 0, "missing_files": 0}
        for source in SOURCES:
            categories = ["공직자재산공개"] if source == "pety" else CATEGORIES
            for category in categories:
                items = self.load_metadata_items(source, category)
                if items:
                    summary["found_files"] += 1
                    summary["found_items"] += len(items)
                else:
                    summary["missing_files"] += 1
        logger.info(
            "아티팩트 점검 결과: 파일 %s개, 아이템 %s개, 누락 %s개",
            summary["found_files"],
            summary["found_items"],
            summary["missing_files"],
        )
        return summary

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
            if not self.hf_token:
                raise RuntimeError("HF_TOKEN 환경변수가 필요합니다.")
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
                token=self.hf_token,
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
@click.option(
    '--create-repo',
    is_flag=True,
    help='HF dataset 리포지터리를 생성(또는 존재 확인)합니다.'
)
@click.option(
    '--private',
    is_flag=True,
    help='--create-repo 사용 시 private 저장소로 생성합니다.'
)
@click.option(
    '--use-bucket-sync',
    is_flag=True,
    help='datasets push 대신 HF storage bucket sync를 사용합니다.'
)
@click.option(
    '--bucket-id',
    default='yakdoli/peti-artifacts',
    help='HF bucket ID (예: yakdoli/peti-artifacts)'
)
@click.option(
    '--bucket-prefix',
    default='searchThema',
    help='bucket 내 업로드 prefix 경로'
)
@click.option(
    '--disable-repo-fallback',
    is_flag=True,
    help='bucket sync 실패 시 repo fallback 업로드를 비활성화합니다.'
)
def main(
    repo_id: str,
    data_dir: str,
    dry_run: bool,
    create_repo: bool,
    private: bool,
    use_bucket_sync: bool,
    bucket_id: str,
    bucket_prefix: str,
    disable_repo_fallback: bool,
) -> None:
    """HuggingFace 데이터셋 업로드"""
    logger.info(f"HuggingFace 업로드 시작")
    logger.info(f"  리포지터리: {repo_id}")
    logger.info(f"  데이터 디렉토리: {data_dir}")
    uploader = HFUploader(repo_id, data_dir)
    logger.info(f"  드라이런: {dry_run}")
    logger.info(f"  리포지터리 생성: {create_repo}")
    logger.info(f"  bucket sync: {use_bucket_sync}")
    logger.info(f"  HF_TOKEN 설정됨: {'yes' if uploader.hf_token else 'no'}")
    logger.info(f"  HF_HUB_DISABLE_XET: {os.getenv('HF_HUB_DISABLE_XET', 'unset')}")
    uploader.validate_artifacts()

    if use_bucket_sync:
        if not uploader.ensure_bucket(bucket_id=bucket_id, private=private, dry_run=dry_run):
            logger.error("bucket 준비 실패로 업로드를 중단합니다.")
            raise SystemExit(1)
        source_dir = Path(data_dir).parent
        ok = uploader.sync_artifacts_to_bucket(
            bucket_id=bucket_id,
            source_dir=source_dir,
            bucket_prefix=bucket_prefix,
            dry_run=dry_run,
            allow_repo_fallback=not disable_repo_fallback,
        )
        if not ok:
            raise SystemExit(1)
        logger.info("HuggingFace bucket sync 종료")
        return

    if create_repo and not uploader.ensure_repo(private=private, dry_run=dry_run):
        logger.error("리포지터리 준비 실패로 업로드를 중단합니다.")
        raise SystemExit(1)
    uploader.upload_all(dry_run=dry_run)

    logger.info(f"HuggingFace 업로드 종료")


if __name__ == '__main__':
    main()
