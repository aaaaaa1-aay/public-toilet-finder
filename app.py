import os
import time
import math
from pathlib import Path

import pandas as pd
import requests
import streamlit as st
import folium
from streamlit_folium import st_folium
from streamlit_geolocation import streamlit_geolocation

# ------------------------------------------------------------
# 기본 설정
# ------------------------------------------------------------
st.set_page_config(
    page_title="우리 동네 공중화장실 찾기",
    page_icon="🚻",
    layout="wide",
    initial_sidebar_state="collapsed",
)

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
SOURCE_FILE = DATA_DIR / "공중화장실정보.csv"
PUBLIC_COORD_FILE = DATA_DIR / "공중화장실좌표.csv"
CACHE_FILE = DATA_DIR / "kakao_coordinate_cache.csv"

KAKAO_ADDRESS_URL = "https://dapi.kakao.com/v2/local/search/address.json"
KAKAO_KEYWORD_URL = "https://dapi.kakao.com/v2/local/search/keyword.json"
DEFAULT_LAT = 37.4979
DEFAULT_LON = 127.0276

st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=Pretendard:wght@400;500;600;700;800&display=swap');
    html, body, [class*="css"] { font-family: Pretendard, sans-serif; }
    .stApp { background: #f5f7fb; }
    .block-container { max-width: 1280px; padding-top: 1.8rem; padding-bottom: 3rem; }
    .hero {
        background: linear-gradient(120deg, #123b32 0%, #16765c 65%, #2c9b76 100%);
        padding: 30px 32px; border-radius: 22px; color: white; margin-bottom: 22px;
        box-shadow: 0 10px 28px rgba(18,59,50,.14);
    }
    .hero h1 { color: white; font-size: 2rem; margin: 0 0 8px 0; font-weight: 800; }
    .hero p { color: #e4f5ee; margin: 0; font-size: 1rem; }
    .metric {
        background: white; padding: 17px 19px; border-radius: 16px;
        border: 1px solid #e8edf2; min-height: 88px;
    }
    .metric-label { color: #718096; font-size: .85rem; margin-bottom: 6px; }
    .metric-value { color: #172b25; font-size: 1.45rem; font-weight: 800; }
    .section-title { font-size: 1.12rem; font-weight: 800; color: #1c3029; margin: 8px 0 12px; }
    div[data-testid="stTextInput"] input { border-radius: 12px; }
    div.stButton > button { border-radius: 11px; font-weight: 700; min-height: 2.7rem; }
    .hint { color: #718096; font-size: .86rem; }
</style>
""", unsafe_allow_html=True)


# ------------------------------------------------------------
# 공통 유틸
# ------------------------------------------------------------
def get_secret(name: str, default: str = "") -> str:
    try:
        value = st.secrets.get(name, default)
        return str(value).strip() if value is not None else default
    except Exception:
        return os.getenv(name, default).strip()


def first_existing_column(df: pd.DataFrame, candidates):
    for name in candidates:
        if name in df.columns:
            return name
    return None


def clean_text(value):
    if pd.isna(value):
        return ""
    return str(value).strip()


def load_csv_safely(path: Path):
    if not path.exists():
        return None, f"파일을 찾을 수 없습니다: {path.name}"
    last_error = None
    for encoding in ("utf-8-sig", "cp949", "euc-kr", "utf-8"):
        try:
            return pd.read_csv(path, encoding=encoding, low_memory=False), None
        except Exception as exc:
            last_error = exc
    return None, f"CSV 파일을 읽지 못했습니다: {last_error}"


def normalize_address(df: pd.DataFrame):
    road_col = first_existing_column(df, ["소재지도로명주소", "도로명주소", "도로명"])
    lot_col = first_existing_column(df, ["소재지지번주소", "지번주소", "소재지주소"])
    if road_col is None and lot_col is None:
        raise ValueError("주소 열을 찾을 수 없습니다. CSV에 소재지도로명주소 또는 소재지지번주소 열이 있는지 확인해 주세요.")
    road = df[road_col].fillna("").astype(str).str.strip() if road_col else pd.Series([""] * len(df), index=df.index)
    lot = df[lot_col].fillna("").astype(str).str.strip() if lot_col else pd.Series([""] * len(df), index=df.index)
    address = road.where(road.ne(""), lot)
    return address, road_col, lot_col


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def kakao_headers(api_key):
    return {"Authorization": f"KakaoAK {api_key}"}


def kakao_address_to_coord(session, address, api_key):
    try:
        response = session.get(
            KAKAO_ADDRESS_URL,
            headers=kakao_headers(api_key),
            params={"query": address, "size": 1},
            timeout=12,
        )
    except requests.RequestException as exc:
        return None, None, f"네트워크 오류: {exc}"

    if response.status_code == 401:
        return None, None, "401 인증 오류: REST API 키를 확인해 주세요."
    if response.status_code == 403:
        return None, None, "403 권한 오류: 카카오 디벨로퍼스의 로컬 API 사용 설정을 확인해 주세요."
    if response.status_code == 429:
        return None, None, "429 요청 한도 초과: 잠시 후 다시 실행해 주세요."
    if response.status_code != 200:
        return None, None, f"HTTP {response.status_code}: {response.text[:250]}"

    try:
        payload = response.json()
    except ValueError:
        return None, None, "카카오 API 응답을 JSON으로 읽을 수 없습니다."

    if payload.get("errorType") or payload.get("code"):
        return None, None, f"카카오 API 오류: {payload.get('message') or payload}"

    documents = payload.get("documents", [])
    if not documents:
        return None, None, "주소 검색 결과 없음"

    try:
        lon = float(documents[0]["x"])
        lat = float(documents[0]["y"])
        return lat, lon, None
    except (KeyError, TypeError, ValueError):
        return None, None, "좌표 응답 형식이 예상과 다릅니다."


def kakao_keyword_search(query, api_key):
    if not api_key:
        return None, None, "검색 장소 좌표를 찾으려면 카카오 REST API 키가 필요합니다."
    try:
        response = requests.get(
            KAKAO_KEYWORD_URL,
            headers=kakao_headers(api_key),
            params={"query": query, "size": 1},
            timeout=8,
        )
        if response.status_code != 200:
            return None, None, f"장소 검색 실패 (HTTP {response.status_code})"
        docs = response.json().get("documents", [])
        if not docs:
            return None, None, "검색한 장소를 찾지 못했습니다."
        return float(docs[0]["y"]), float(docs[0]["x"]), None
    except Exception as exc:
        return None, None, f"장소 검색 오류: {exc}"


# ------------------------------------------------------------
# 데이터 읽기: 공개 사용자는 GitHub에 올라간 좌표 파일을 사용
# ------------------------------------------------------------
@st.cache_data(show_spinner=False)
def load_public_data(file_mtime):
    df, error = load_csv_safely(PUBLIC_COORD_FILE)
    if df is None:
        return None, error
    required = {"위도", "경도"}
    if not required.issubset(set(df.columns)):
        return None, "좌표 파일에 위도/경도 열이 없습니다."
    df["위도"] = pd.to_numeric(df["위도"], errors="coerce")
    df["경도"] = pd.to_numeric(df["경도"], errors="coerce")
    df = df.dropna(subset=["위도", "경도"]).copy()
    return df, None


def get_operating_color(row):
    status_cols = ["운영여부", "개방여부", "운영상태", "화장실개방여부", "개방상태"]
    text = " ".join(clean_text(row.get(c, "")) for c in status_cols).lower()
    if any(word in text for word in ["폐쇄", "미운영", "운영중단", "미개방", "사용불가"]):
        return "gray"
    if any(word in text for word in ["운영", "개방", "상시", "가능"]):
        return "green"
    return "orange"


def popup_html(row, address_col, time_col, owner_col):
    address = clean_text(row.get(address_col, "")) if address_col else ""
    open_time = clean_text(row.get(time_col, "")) if time_col else ""
    owner = clean_text(row.get(owner_col, "")) if owner_col else ""
    # 사용자 요청대로 세 항목만 표시
    return f"""
    <div style="font-family:Arial,sans-serif;width:230px;line-height:1.55">
      <b>주소</b><br>{address or '정보 없음'}<hr>
      <b>개방시간상세</b><br>{open_time or '정보 없음'}<hr>
      <b>화장실소유</b><br>{owner or '정보 없음'}
    </div>
    """


# ------------------------------------------------------------
# 헤더
# ------------------------------------------------------------
st.markdown("""
<div class="hero">
  <h1>🚻 우리 동네 공중화장실 찾기</h1>
  <p>전국 공중화장실 위치와 개방 정보를 지도에서 간편하게 확인하세요.</p>
</div>
""", unsafe_allow_html=True)

api_key = get_secret("KAKAO_REST_API_KEY")
admin_password = get_secret("ADMIN_PASSWORD")

# 공개용 데이터가 없으면 원본 CSV만 보여주고, 관리자에게 변환 안내
public_df = None
load_error = None
if PUBLIC_COORD_FILE.exists():
    public_df, load_error = load_public_data(PUBLIC_COORD_FILE.stat().st_mtime)
else:
    load_error = "아직 전국 좌표 데이터가 등록되지 않았습니다."

if public_df is None:
    st.warning("지도에 표시할 전국 좌표 데이터가 아직 준비되지 않았습니다. 관리자 메뉴에서 주소를 좌표로 변환한 뒤, 다운로드한 CSV를 GitHub의 data 폴더에 업로드해 주세요.")
    public_df = pd.DataFrame()

# 주요 컬럼
address_col = first_existing_column(public_df, ["소재지도로명주소", "도로명주소", "소재지지번주소", "지번주소"])
time_col = first_existing_column(public_df, ["개방시간상세", "개방시간", "개방시간상세내용"])
owner_col = first_existing_column(public_df, ["화장실소유구분명", "화장실소유", "소유구분명"])

total_count = len(public_df)
col1, col2, col3 = st.columns(3)
with col1:
    st.markdown(f'<div class="metric"><div class="metric-label">지도 등록 화장실</div><div class="metric-value">{total_count:,}곳</div></div>', unsafe_allow_html=True)
with col2:
    st.markdown(f'<div class="metric"><div class="metric-label">검색 방식</div><div class="metric-value">지역 · 장소</div></div>', unsafe_allow_html=True)
with col3:
    st.markdown(f'<div class="metric"><div class="metric-label">좌표 데이터</div><div class="metric-value">공유형</div></div>', unsafe_allow_html=True)

# 브라우저 현재 위치를 사용합니다. 위치 권한을 거부하거나 사용할 수 없으면 강남역을 기본 위치로 사용합니다.
user_lat, user_lon = None, None
try:
    location_data = streamlit_geolocation()
    if isinstance(location_data, dict):
        raw_lat = location_data.get("latitude")
        raw_lon = location_data.get("longitude")
        if raw_lat is not None and raw_lon is not None:
            user_lat, user_lon = float(raw_lat), float(raw_lon)
except Exception:
    user_lat, user_lon = None, None

st.markdown('<div class="section-title">어디의 화장실을 찾으세요?</div>', unsafe_allow_html=True)
search_col, search_btn_col = st.columns([5, 1])
with search_col:
    search_query = st.text_input(
        "지역 또는 장소 검색",
        placeholder="예: 경기도, 성남시, 강남역",
        label_visibility="collapsed",
    )
with search_btn_col:
    search_clicked = st.button("검색", use_container_width=True, type="primary")

# 검색은 주소에 포함된 지역명 우선, 장소명은 Kakao 키워드 검색 후 반경 검색
filtered = public_df.copy()
center_lat, center_lon = (user_lat, user_lon) if user_lat is not None and user_lon is not None else (DEFAULT_LAT, DEFAULT_LON)
search_message = ""

if not public_df.empty and search_query.strip():
    q = search_query.strip()
    searchable_cols = [c for c in ["소재지도로명주소", "소재지지번주소", "도로명주소", "지번주소"] if c in public_df.columns]
    if searchable_cols:
        mask = pd.Series(False, index=public_df.index)
        for c in searchable_cols:
            mask = mask | public_df[c].fillna("").astype(str).str.contains(q, case=False, regex=False)
        matched = public_df[mask].copy()
    else:
        matched = pd.DataFrame()

    if not matched.empty:
        filtered = matched
        center_lat = float(filtered["위도"].median())
        center_lon = float(filtered["경도"].median())
        search_message = f"'{q}' 주소 검색 결과 {len(filtered):,}곳"
    else:
        place_lat, place_lon, place_error = kakao_keyword_search(q, api_key)
        if place_lat is not None:
            distances = public_df.apply(
                lambda r: haversine_km(place_lat, place_lon, float(r["위도"]), float(r["경도"])),
                axis=1,
            )
            filtered = public_df[distances <= 5].copy()
            center_lat, center_lon = place_lat, place_lon
            search_message = f"'{q}' 주변 5km 검색 결과 {len(filtered):,}곳"
        else:
            filtered = public_df.iloc[0:0].copy()
            search_message = place_error or "검색 결과가 없습니다."

if search_query.strip():
    st.caption(search_message)

# 지도
st.markdown('<div class="section-title">화장실 위치 지도</div>', unsafe_allow_html=True)
map_obj = folium.Map(location=[center_lat, center_lon], zoom_start=12 if search_query.strip() else 7, control_scale=True)

if not filtered.empty:
    # 지도 렌더링이 과도하게 느려지지 않도록 최대 2500개 표시
    map_rows = filtered.head(2500)
    for _, row in map_rows.iterrows():
        lat, lon = float(row["위도"]), float(row["경도"])
        color = get_operating_color(row)
        folium.Marker(
            location=[lat, lon],
            tooltip=clean_text(row.get(address_col, "공중화장실")) if address_col else "공중화장실",
            popup=folium.Popup(popup_html(row, address_col, time_col, owner_col), max_width=280),
            icon=folium.Icon(color=color, icon="info-sign"),
        ).add_to(map_obj)
    if len(filtered) > 2500:
        st.info("표시 속도를 위해 지도에는 검색 결과 중 최대 2,500곳만 표시하고 있습니다. 아래 목록에는 전체 검색 결과가 표시됩니다.")
else:
    folium.Marker(
        [center_lat, center_lon],
        tooltip="기본 위치",
        popup="검색어에 해당하는 화장실 위치가 없습니다.",
        icon=folium.Icon(color="blue", icon="map-marker"),
    ).add_to(map_obj)

st_folium(map_obj, width=None, height=590, returned_objects=[], key="public_toilet_map")

# 목록은 요청한 세 항목만 표시
st.markdown('<div class="section-title">검색 결과</div>', unsafe_allow_html=True)
if not filtered.empty:
    display_df = pd.DataFrame()
    display_df["주소"] = filtered[address_col].fillna("") if address_col else ""
    display_df["개방시간상세"] = filtered[time_col].fillna("") if time_col else ""
    display_df["화장실소유"] = filtered[owner_col].fillna("") if owner_col else ""
    st.dataframe(display_df, use_container_width=True, hide_index=True, height=300)
else:
    st.info("표시할 화장실이 없습니다. 지역명을 검색하거나, 아직 좌표 데이터가 등록되지 않았는지 확인해 주세요.")

# ------------------------------------------------------------
# 관리자 전용: 주소 -> 좌표 변환, 결과 파일 다운로드
# ------------------------------------------------------------
with st.expander("🔐 관리자 메뉴 (전국 주소 좌표 변환)", expanded=False):
    st.caption("관리자만 사용하세요. 변환이 끝나면 좌표 CSV를 다운로드해 GitHub 저장소의 data 폴더에 업로드해야 모든 사용자에게 공유됩니다.")
    if not admin_password:
        st.error("Streamlit Secrets에 ADMIN_PASSWORD를 등록해 주세요.")
    else:
        entered_password = st.text_input("관리자 비밀번호", type="password", key="admin_pw")
        is_admin = entered_password == admin_password
        if not is_admin:
            st.caption("관리자 비밀번호를 입력하면 좌표 변환 기능이 열립니다.")
        else:
            st.success("관리자 인증 완료")
            source_df, source_error = load_csv_safely(SOURCE_FILE)
            if source_df is None:
                st.error(source_error)
            else:
                try:
                    addresses, road_col, lot_col = normalize_address(source_df)
                    source_df = source_df.copy()
                    source_df["주소_좌표변환용"] = addresses
                    valid_addresses = source_df["주소_좌표변환용"].fillna("").astype(str).str.strip()
                    unique_addresses = sorted(set(a for a in valid_addresses if a))
                    st.write(f"원본 데이터: **{len(source_df):,}행** · 좌표 변환 대상 주소: **{len(unique_addresses):,}개**")

                    existing_cache, _ = load_csv_safely(CACHE_FILE)
                    if existing_cache is None:
                        existing_cache = pd.DataFrame(columns=["주소_좌표변환용", "위도", "경도", "변환상태"])
                    for col in ["주소_좌표변환용", "위도", "경도", "변환상태"]:
                        if col not in existing_cache.columns:
                            existing_cache[col] = None
                    existing_cache["위도"] = pd.to_numeric(existing_cache["위도"], errors="coerce")
                    existing_cache["경도"] = pd.to_numeric(existing_cache["경도"], errors="coerce")
                    cache_map = {}
                    for _, r in existing_cache.iterrows():
                        addr = clean_text(r["주소_좌표변환용"])
                        if addr and pd.notna(r["위도"]) and pd.notna(r["경도"]):
                            cache_map[addr] = (float(r["위도"]), float(r["경도"]))

                    done_count = sum(1 for a in unique_addresses if a in cache_map)
                    st.write(f"기존 좌표 캐시: **{done_count:,}개** · 새로 변환할 주소: **{len(unique_addresses)-done_count:,}개**")

                    if not api_key:
                        st.error("KAKAO_REST_API_KEY가 Secrets에 없습니다. Streamlit Cloud의 Secrets 설정을 확인해 주세요.")
                    elif st.button("전국 주소 좌표 변환 시작", type="primary", use_container_width=True, key="start_geocoding"):
                        pending = [a for a in unique_addresses if a not in cache_map]
                        if not pending:
                            st.success("모든 주소의 좌표가 이미 준비되어 있습니다.")
                        else:
                            progress = st.progress(0.0)
                            status_box = st.empty()
                            error_box = st.empty()
                            session = requests.Session()
                            errors = []
                            success_count = 0
                            stopped = False

                            for idx, address in enumerate(pending, start=1):
                                lat, lon, error = kakao_address_to_coord(session, address, api_key)
                                if error:
                                    errors.append((address, error))
                                    # 주소 검색 결과 없음은 기록하고 계속, 인증/한도/네트워크 오류는 중단
                                    if error.startswith(("401", "403", "429", "HTTP", "네트워크 오류")):
                                        stopped = True
                                        status_box.error(f"{idx:,}/{len(pending):,} 처리 중 중단: {error}")
                                        break
                                else:
                                    cache_map[address] = (lat, lon)
                                    success_count += 1

                                progress.progress(idx / len(pending))
                                if idx % 10 == 0 or idx == len(pending):
                                    status_box.info(f"진행 중: {idx:,}/{len(pending):,} · 이번 실행 성공 {success_count:,}개")
                                # 과도한 요청을 피하기 위한 짧은 간격
                                time.sleep(0.12)

                                # 100개마다 중간 저장하여 중단되어도 진행분 보존
                                if idx % 100 == 0:
                                    partial = pd.DataFrame(
                                        [{"주소_좌표변환용": a, "위도": v[0], "경도": v[1], "변환상태": "성공"} for a, v in cache_map.items()]
                                    )
                                    partial.to_csv(CACHE_FILE, index=False, encoding="utf-8-sig")

                            cache_out = pd.DataFrame(
                                [{"주소_좌표변환용": a, "위도": v[0], "경도": v[1], "변환상태": "성공"} for a, v in cache_map.items()]
                            )
                            cache_out.to_csv(CACHE_FILE, index=False, encoding="utf-8-sig")

                            # 원본 각 행에 좌표 붙이기
                            source_df["위도"] = source_df["주소_좌표변환용"].map(lambda a: cache_map.get(clean_text(a), (None, None))[0])
                            source_df["경도"] = source_df["주소_좌표변환용"].map(lambda a: cache_map.get(clean_text(a), (None, None))[1])
                            result_df = source_df.dropna(subset=["위도", "경도"]).copy()
                            result_df["위도"] = pd.to_numeric(result_df["위도"], errors="coerce")
                            result_df["경도"] = pd.to_numeric(result_df["경도"], errors="coerce")
                            result_df.to_csv(PUBLIC_COORD_FILE, index=False, encoding="utf-8-sig")

                            st.cache_data.clear()
                            if stopped:
                                st.warning("일부 주소만 변환되었습니다. 원인을 해결한 뒤 다시 실행하면 저장된 좌표를 건너뛰고 이어서 변환합니다.")
                            else:
                                st.success("주소 좌표 변환이 완료되었습니다.")
                            st.write(f"이번 실행 성공: **{success_count:,}개** · 전체 좌표가 있는 행: **{len(result_df):,}개** · 변환 실패/검색 결과 없음: **{len(errors):,}개**")
                            if errors:
                                with st.expander("변환 실패 주소 예시"):
                                    st.dataframe(pd.DataFrame(errors[:30], columns=["주소", "오류"]), use_container_width=True, hide_index=True)

                    # 이미 생성된 파일은 언제든 내려받을 수 있음
                    if PUBLIC_COORD_FILE.exists():
                        with open(PUBLIC_COORD_FILE, "rb") as f:
                            st.download_button(
                                "전국 좌표 데이터 CSV 다운로드",
                                data=f.read(),
                                file_name="공중화장실좌표.csv",
                                mime="text/csv",
                                use_container_width=True,
                                key="download_public_csv",
                            )
                    if CACHE_FILE.exists():
                        with open(CACHE_FILE, "rb") as f:
                            st.download_button(
                                "좌표 변환 캐시 다운로드",
                                data=f.read(),
                                file_name="kakao_coordinate_cache.csv",
                                mime="text/csv",
                                use_container_width=True,
                                key="download_cache_csv",
                            )
                    st.info("중요: 다운로드한 공중화장실좌표.csv를 GitHub의 data 폴더에 업로드하고 커밋해야 다른 사용자에게도 위치가 공유됩니다. Streamlit Cloud의 임시 파일만으로는 영구 공유되지 않습니다.")
                except Exception as exc:
                    st.exception(exc)

st.markdown("---")
st.caption("화장실 위치와 운영 정보는 제공된 공공데이터를 바탕으로 하며, 현장 상황과 다를 수 있습니다.")
