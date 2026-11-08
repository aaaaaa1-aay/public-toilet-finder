import os
import re
import html
import math
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st
import folium

from folium.plugins import MarkerCluster
from streamlit_folium import st_folium
from streamlit_geolocation import streamlit_geolocation


# =========================================================
# 1. 기본 설정
# =========================================================

st.set_page_config(
    page_title="전국 공중화장실 찾기",
    page_icon="🚻",
    layout="wide"
)

st.title("🚻 전국 공중화장실 찾기")
st.caption("검색하거나 현재 위치를 기준으로 주변 공중화장실을 찾아보세요.")

DATA_PATH = "data/공중화장실정보.csv"
CACHE_PATH = "data/kakao_coordinate_cache.csv"

KAKAO_ADDRESS_URL = (
    "https://dapi.kakao.com/v2/local/search/address.json"
)
KAKAO_KEYWORD_URL = (
    "https://dapi.kakao.com/v2/local/search/keyword.json"
)

# 위치를 사용할 수 없을 때 기본 위치: 강남역
GANGNAM_LAT = 37.498095
GANGNAM_LON = 127.027610

# 현재 위치 검색 시 주변 화장실 검색 반경
SEARCH_RADIUS_KM = 10

SEOUL_TIMEZONE = ZoneInfo("Asia/Seoul")


# =========================================================
# 2. 공통 함수
# =========================================================

def clean_text(value):
    """빈 값과 NaN을 안전하게 문자열로 변환."""
    if pd.isna(value):
        return ""
    return str(value).strip()


def get_address(row):
    """도로명주소를 우선 사용하고, 없으면 지번주소 사용."""
    road = clean_text(row.get("소재지도로명주소", ""))
    lot = clean_text(row.get("소재지지번주소", ""))

    return road if road else lot


def escape_text(value):
    """팝업에 표시할 문자열의 HTML 특수문자 처리."""
    return html.escape(clean_text(value))


# =========================================================
# 3. 전국 공중화장실 CSV 불러오기
# =========================================================

@st.cache_data
def load_toilet_data():
    if not os.path.exists(DATA_PATH):
        raise FileNotFoundError(
            f"{DATA_PATH} 파일을 찾을 수 없습니다."
        )

    encodings = ["utf-8-sig", "cp949", "euc-kr"]

    for encoding in encodings:
        try:
            df = pd.read_csv(
                DATA_PATH,
                encoding=encoding,
                dtype=str,
                low_memory=False
            )
            return df.fillna("")
        except UnicodeDecodeError:
            continue

    raise ValueError("CSV 파일의 인코딩을 확인할 수 없습니다.")


# =========================================================
# 4. 좌표 캐시 불러오기 및 저장
# =========================================================

def load_coordinate_cache():
    """
    이미 변환한 주소의 좌표를 불러온다.
    캐시에 없는 주소만 카카오 API로 요청한다.
    """
    if not os.path.exists(CACHE_PATH):
        return {}

    try:
        cache_df = pd.read_csv(
            CACHE_PATH,
            encoding="utf-8-sig",
            dtype=str
        )

        required_columns = {"주소", "위도", "경도"}

        if not required_columns.issubset(cache_df.columns):
            return {}

        cache = {}

        for _, row in cache_df.iterrows():
            address = clean_text(row["주소"])

            try:
                lat = float(row["위도"])
                lon = float(row["경도"])

                if address:
                    cache[address] = (lat, lon)

            except (TypeError, ValueError):
                continue

        return cache

    except Exception:
        return {}


def save_coordinate_cache(cache):
    """변환에 성공한 좌표를 CSV 캐시 파일로 저장한다."""
    os.makedirs("data", exist_ok=True)

    rows = []

    for address, coordinates in cache.items():
        lat, lon = coordinates

        rows.append({
            "주소": address,
            "위도": lat,
            "경도": lon
        })

    cache_df = pd.DataFrame(
        rows,
        columns=["주소", "위도", "경도"]
    )

    cache_df.to_csv(
        CACHE_PATH,
        index=False,
        encoding="utf-8-sig"
    )


# =========================================================
# 5. 카카오 주소 검색 API
# =========================================================

def kakao_address_to_coordinate(address, api_key):
    """
    주소를 카카오 API에 전달해 위도와 경도를 가져온다.

    반환:
      (위도, 경도) : 성공
      None         : 검색 결과 없음 또는 일반 오류
      "LIMIT"      : 요청 한도 초과
      "AUTH_ERROR" : API 키 또는 권한 오류
    """

    if not address:
        return None

    headers = {
        "Authorization": f"KakaoAK {api_key}"
    }

    try:
        response = requests.get(
            KAKAO_ADDRESS_URL,
            headers=headers,
            params={"query": address},
            timeout=10
        )

        if response.status_code == 429:
            return "LIMIT"

        if response.status_code in (401, 403):
            return "AUTH_ERROR"

        if response.status_code != 200:
            return None

        documents = response.json().get("documents", [])

        if not documents:
            return None

        result = documents[0]

        lon = float(result["x"])
        lat = float(result["y"])

        return (lat, lon)

    except (requests.RequestException, ValueError, KeyError):
        return None


# =========================================================
# 6. 전국 주소 좌표 변환
# =========================================================

def convert_all_addresses(df, api_key, cache):
    """
    전국 데이터의 고유 주소를 좌표로 변환한다.

    이미 캐시에 있는 주소는 API를 다시 호출하지 않는다.
    중간에 한도 초과가 발생해도 성공한 결과는 저장한다.
    """

    addresses = df.apply(get_address, axis=1)

    unique_addresses = sorted({
        address
        for address in addresses
        if address
    })

    pending_addresses = [
        address
        for address in unique_addresses
        if address not in cache
    ]

    if not pending_addresses:
        st.success("모든 주소의 좌표가 이미 준비되어 있습니다.")
        return cache

    st.info(
        f"전체 고유 주소 {len(unique_addresses):,}개 중 "
        f"{len(pending_addresses):,}개를 새로 변환합니다."
    )

    progress = st.progress(0)
    status = st.empty()

    success_count = 0
    fail_count = 0
    stopped_by_limit = False
    auth_error = False

    total = len(pending_addresses)

    for index, address in enumerate(pending_addresses, start=1):

        result = kakao_address_to_coordinate(
            address,
            api_key
        )

        if result == "LIMIT":
            stopped_by_limit = True
            status.warning(
                "카카오 API 요청 한도에 도달했습니다. "
                "성공한 좌표는 저장하고 변환을 중단합니다."
            )
            break

        if result == "AUTH_ERROR":
            auth_error = True
            status.error(
                "카카오 API 인증에 실패했습니다. "
                "REST API 키와 카카오맵 사용 설정을 확인하세요."
            )
            break

        if result is None:
            fail_count += 1
        else:
            cache[address] = result
            success_count += 1

        progress.progress(index / total)

        status.text(
            f"변환 진행: {index:,}/{total:,} | "
            f"성공: {success_count:,} | "
            f"실패: {fail_count:,}"
        )

        # 일정 개수마다 중간 저장
        if index % 20 == 0:
            save_coordinate_cache(cache)

    save_coordinate_cache(cache)

    progress.empty()

    if auth_error:
        st.error("인증 문제로 좌표 변환을 중단했습니다.")

    elif stopped_by_limit:
        st.warning(
            "요청 한도 때문에 일부 주소만 변환했습니다. "
            "한도가 다시 제공된 후 남은 주소를 이어서 변환할 수 있습니다."
        )

    else:
        st.success(
            f"좌표 변환 완료! 성공 {success_count:,}개, "
            f"실패 {fail_count:,}개"
        )

    return cache


# =========================================================
# 7. 운영 상태 판별
# =========================================================

def get_open_status(row):
    """
    개방시간상세를 기준으로 운영 상태를 간단히 판별한다.

    초록색: 운영 중
    회색: 운영 종료
    주황색: 운영 정보 불명
    """

    detail = clean_text(row.get("개방시간상세", ""))

    if not detail:
        return "unknown"

    text = detail.replace(" ", "")

    # 상시 개방으로 판단할 수 있는 표현
    always_open_words = [
        "24시간",
        "상시개방",
        "연중무휴",
        "24시개방"
    ]

    if any(word in text for word in always_open_words):
        return "open"

    # 시간 범위 찾기: 09:00~18:00, 09:00-18:00 등
    time_pattern = (
        r"(\d{1,2}:\d{2})\s*[~\-–∼]\s*(\d{1,2}:\d{2})"
    )

    matches = re.findall(time_pattern, detail)

    if not matches:
        return "unknown"

    now = datetime.now(SEOUL_TIMEZONE).time()

    for start_text, end_text in matches:
        try:
            start_hour, start_minute = map(
                int, start_text.split(":")
            )
            end_hour, end_minute = map(
                int, end_text.split(":")
            )

            if (
                start_hour > 23 or end_hour > 23
                or start_minute > 59 or end_minute > 59
            ):
                continue

            start = datetime.strptime(
                start_text, "%H:%M"
            ).time()

            end = datetime.strptime(
                end_text, "%H:%M"
            ).time()

            # 자정을 넘기는 운영시간
            if start > end:
                if now >= start or now <= end:
                    return "open"
            else:
                if start <= now <= end:
                    return "open"

        except ValueError:
            continue

    return "closed"


def get_marker_color(status):
    if status == "open":
        return "green"

    if status == "closed":
        return "gray"

    return "orange"


# =========================================================
# 8. 거리 계산
# =========================================================

def calculate_distance_km(lat1, lon1, lat2, lon2):
    """두 좌표 사이의 직선거리를 km 단위로 계산한다."""

    radius = 6371

    lat1 = math.radians(lat1)
    lon1 = math.radians(lon1)
    lat2 = math.radians(lat2)
    lon2 = math.radians(lon2)

    dlat = lat2 - lat1
    dlon = lon2 - lon1

    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1)
        * math.cos(lat2)
        * math.sin(dlon / 2) ** 2
    )

    return 2 * radius * math.asin(math.sqrt(a))


# =========================================================
# 9. 검색어를 장소 좌표로 변환
# =========================================================

def kakao_keyword_search(keyword, api_key):
    """강남역 등 장소 검색어의 중심 좌표를 찾는다."""

    headers = {
        "Authorization": f"KakaoAK {api_key}"
    }

    try:
        response = requests.get(
            KAKAO_KEYWORD_URL,
            headers=headers,
            params={"query": keyword, "size": 1},
            timeout=10
        )

        if response.status_code != 200:
            return None

        documents = response.json().get("documents", [])

        if not documents:
            return None

        result = documents[0]

        return (
            float(result["y"]),
            float(result["x"])
        )

    except (requests.RequestException, ValueError, KeyError):
        return None


# =========================================================
# 10. 화장실 마커 팝업
# =========================================================

def make_popup_html(row):
    """
    사용자가 요청한 3가지 정보만 표시한다.
    주소 / 개방시간상세 / 화장실소유
    """

    address = get_address(row) or "주소 정보 없음"

    opening_hours = (
        clean_text(row.get("개방시간상세", ""))
        or "정보 없음"
    )

    ownership = (
        clean_text(row.get("화장실소유구분명", ""))
        or "정보 없음"
    )

    return f"""
    <div style="
        width: 240px;
        font-size: 13px;
        line-height: 1.6;
        word-break: break-all;
    ">
        <b>주소</b><br>
        {escape_text(address)}
        <hr style="margin: 7px 0;">
        <b>개방시간상세</b><br>
        {escape_text(opening_hours)}
        <hr style="margin: 7px 0;">
        <b>화장실소유</b><br>
        {escape_text(ownership)}
    </div>
    """


# =========================================================
# 11. 지도 생성
# =========================================================

def create_toilet_map(
    toilet_df,
    center_lat,
    center_lon,
    zoom_start=13,
    show_user_location=False
):
    toilet_map = folium.Map(
        location=[center_lat, center_lon],
        zoom_start=zoom_start,
        control_scale=True,
        tiles="OpenStreetMap"
    )

    # 현재 위치 표시
    if show_user_location:
        folium.Marker(
            location=[center_lat, center_lon],
            tooltip="현재 위치",
            popup="현재 위치",
            icon=folium.Icon(
                color="blue",
                icon="user",
                prefix="fa"
            )
        ).add_to(toilet_map)

        folium.Circle(
            location=[center_lat, center_lon],
            radius=SEARCH_RADIUS_KM * 1000,
            color="blue",
            fill=False,
            weight=1
        ).add_to(toilet_map)

    marker_cluster = MarkerCluster(
        name="공중화장실"
    ).add_to(toilet_map)

    for _, row in toilet_df.iterrows():

        try:
            lat = float(row["위도"])
            lon = float(row["경도"])
        except (ValueError, TypeError):
            continue

        status = get_open_status(row)
        color = get_marker_color(status)

        popup = folium.Popup(
            make_popup_html(row),
            max_width=280
        )

        folium.Marker(
            location=[lat, lon],
            popup=popup,
            tooltip="공중화장실",
            icon=folium.Icon(
                color=color,
                icon="info-sign"
            )
        ).add_to(marker_cluster)

    folium.LayerControl().add_to(toilet_map)

    return toilet_map


# =========================================================
# 12. 데이터 준비
# =========================================================

try:
    toilet_df = load_toilet_data()

except FileNotFoundError as error:
    st.error(str(error))
    st.info(
        "GitHub 저장소에서 data 폴더 안에 "
        "공중화장실정보.csv 파일이 있는지 확인하세요."
    )
    st.stop()

except Exception as error:
    st.error(f"데이터를 불러오는 중 오류가 발생했습니다: {error}")
    st.stop()


required_columns = [
    "소재지도로명주소",
    "소재지지번주소",
    "개방시간상세",
    "화장실소유구분명"
]

missing_columns = [
    column
    for column in required_columns
    if column not in toilet_df.columns
]

if missing_columns:
    st.error(
        "CSV에 필요한 열이 없습니다: "
        + ", ".join(missing_columns)
    )
    st.stop()


# 좌표 캐시 로드
coordinate_cache = load_coordinate_cache()

# CSV 주소를 기준으로 좌표 연결
toilet_df["주소"] = toilet_df.apply(get_address, axis=1)

toilet_df["위도"] = toilet_df["주소"].map(
    lambda address: (
        coordinate_cache[address][0]
        if address in coordinate_cache else None
    )
)

toilet_df["경도"] = toilet_df["주소"].map(
    lambda address: (
        coordinate_cache[address][1]
        if address in coordinate_cache else None
    )
)


# =========================================================
# 13. 사이드바 - API 키와 좌표 변환
# =========================================================

st.sidebar.header("⚙️ 설정")

api_key = st.sidebar.text_input(
    "카카오 REST API 키",
    type="password",
    help="카카오 개발자 페이지의 REST API 키를 입력하세요."
)

st.sidebar.caption(
    "API 키는 GitHub 코드에 직접 입력하지 마세요."
)

st.sidebar.divider()

total_addresses = len({
    address
    for address in toilet_df["주소"]
    if address
})

converted_addresses = len(coordinate_cache)

st.sidebar.metric(
    "전체 고유 주소",
    f"{total_addresses:,}개"
)

st.sidebar.metric(
    "좌표 변환 완료",
    f"{converted_addresses:,}개"
)

st.sidebar.metric(
    "좌표 변환 대기",
    f"{max(total_addresses - converted_addresses, 0):,}개"
)

if st.sidebar.button(
    "전국 주소 좌표 변환 시작",
    use_container_width=True
):
    if not api_key.strip():
        st.sidebar.error("카카오 REST API 키를 먼저 입력하세요.")

    else:
        coordinate_cache = convert_all_addresses(
            toilet_df,
            api_key.strip(),
            coordinate_cache
        )

        st.cache_data.clear()
        st.rerun()


# =========================================================
# 14. 검색창과 현재 위치
# =========================================================

st.subheader("🔎 화장실 검색")

search_query = st.text_input(
    "지역 또는 장소 검색",
    placeholder="예: 경기도, 성남시, 강남역"
)

st.caption(
    "검색어를 입력하지 않으면 현재 위치 주변을 검색합니다. "
    "위치 정보를 사용할 수 없으면 강남역을 기준으로 검색합니다."
)

st.subheader("📍 현재 위치")

location_data = streamlit_geolocation()

user_lat = None
user_lon = None

if isinstance(location_data, dict):
    try:
        user_lat = float(location_data.get("latitude"))
        user_lon = float(location_data.get("longitude"))
    except (TypeError, ValueError):
        user_lat = None
        user_lon = None

if user_lat is not None and user_lon is not None:
    default_lat = user_lat
    default_lon = user_lon
    location_label = "현재 위치"

else:
    default_lat = GANGNAM_LAT
    default_lon = GANGNAM_LON
    location_label = "강남역 (기본 위치)"


# =========================================================
# 15. 좌표가 준비되지 않은 경우
# =========================================================

toilet_df = toilet_df.dropna(
    subset=["위도", "경도"]
).copy()

toilet_df["위도"] = pd.to_numeric(
    toilet_df["위도"],
    errors="coerce"
)

toilet_df["경도"] = pd.to_numeric(
    toilet_df["경도"],
    errors="coerce"
)

toilet_df = toilet_df.dropna(
    subset=["위도", "경도"]
)

if toilet_df.empty:
    st.warning(
        "아직 지도에 표시할 좌표가 없습니다. "
        "왼쪽 설정에서 카카오 REST API 키를 입력하고 "
        "'전국 주소 좌표 변환 시작'을 눌러주세요."
    )
    st.stop()


# =========================================================
# 16. 검색 결과 필터링
# =========================================================

result_df = toilet_df.copy()

map_lat = default_lat
map_lon = default_lon
map_zoom = 13
show_user_location = not bool(search_query.strip())

if search_query.strip():

    query = search_query.strip()

    # 먼저 CSV 주소에서 검색
    address_mask = (
        result_df["소재지도로명주소"].fillna("").str.contains(
            query, case=False, regex=False
        )
        |
        result_df["소재지지번주소"].fillna("").str.contains(
            query, case=False, regex=False
        )
    )

    region_results = result_df[address_mask].copy()

    if not region_results.empty:

        result_df = region_results

        map_lat = result_df["위도"].astype(float).mean()
        map_lon = result_df["경도"].astype(float).mean()
        map_zoom = 9

    else:
        # 주소에 검색어가 없으면 장소 검색으로 중심 좌표 찾기
        if not api_key.strip():
            st.warning(
                "장소 검색을 하려면 카카오 REST API 키가 필요합니다."
            )
            st.stop()

        place_coordinate = kakao_keyword_search(
            query,
            api_key.strip()
        )

        if place_coordinate is None:
            st.warning(
                "검색 결과를 찾지 못했습니다. "
                "지역명이나 장소명을 다시 확인해 주세요."
            )
            st.stop()

        map_lat, map_lon = place_coordinate
        map_zoom = 14

        # 검색 장소 주변 10km 이내 화장실
        result_df["거리(km)"] = result_df.apply(
            lambda row: calculate_distance_km(
                map_lat,
                map_lon,
                float(row["위도"]),
                float(row["경도"])
            ),
            axis=1
        )

        result_df = result_df[
            result_df["거리(km)"] <= SEARCH_RADIUS_KM
        ].copy()

        result_df = result_df.sort_values("거리(km)")

else:
    # 검색어가 없으면 현재 위치 또는 강남역 주변 검색
    result_df["거리(km)"] = result_df.apply(
        lambda row: calculate_distance_km(
            default_lat,
            default_lon,
            float(row["위도"]),
            float(row["경도"])
        ),
        axis=1
    )

    result_df = result_df[
        result_df["거리(km)"] <= SEARCH_RADIUS_KM
    ].copy()

    result_df = result_df.sort_values("거리(km)")


# =========================================================
# 17. 검색 결과 및 지도 출력
# =========================================================

st.divider()

if search_query.strip():
    st.subheader(f"📍 '{search_query}' 검색 결과")

else:
    st.subheader(f"📍 {location_label} 주변 공중화장실")

if result_df.empty:
    st.warning(
        "해당 위치 주변에서 좌표가 확인된 화장실을 찾지 못했습니다."
    )
    st.info(
        "좌표 변환이 완료되지 않은 주소가 남아 있는지도 확인해 주세요."
    )
    st.stop()


# 운영 상태별 개수
open_count = sum(
    get_open_status(row) == "open"
    for _, row in result_df.iterrows()
)

closed_count = sum(
    get_open_status(row) == "closed"
    for _, row in result_df.iterrows()
)

unknown_count = len(result_df) - open_count - closed_count

col1, col2, col3, col4 = st.columns(4)

col1.metric("검색된 화장실", f"{len(result_df):,}개")
col2.metric("운영 중", f"{open_count:,}개")
col3.metric("미운영", f"{closed_count:,}개")
col4.metric("운영 정보 불명", f"{unknown_count:,}개")


# 지도 범례
st.markdown(
    """
    🟢 운영 중　　⚪ 미운영　　🟠 운영 정보 불명
    """
)

toilet_map = create_toilet_map(
    result_df,
    map_lat,
    map_lon,
    zoom_start=map_zoom,
    show_user_location=show_user_location
)

st_folium(
    toilet_map,
    width=None,
    height=650,
    returned_objects=[]
)


# =========================================================
# 18. 검색 결과 목록
# =========================================================

st.subheader("📋 검색 결과")

# 목록에도 요청한 3개 정보만 표시
display_df = pd.DataFrame({
    "주소": result_df.apply(get_address, axis=1),
    "개방시간상세": result_df["개방시간상세"].apply(
        lambda value: clean_text(value) or "정보 없음"
    ),
    "화장실소유": result_df["화장실소유구분명"].apply(
        lambda value: clean_text(value) or "정보 없음"
    )
})

st.dataframe(
    display_df.reset_index(drop=True),
    use_container_width=True,
    hide_index=True
)

st.caption(
    "지도 마커를 클릭하면 주소, 개방시간상세, 화장실소유 정보를 확인할 수 있습니다."
)
