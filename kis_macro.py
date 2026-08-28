import sys
import time
import datetime
import math
import os

import yaml
import requests

# ─── [1. 전역 설정 및 인자 파싱] ───
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kis_info.yaml")


def load_kis_config():
    if not os.path.exists(CONFIG_PATH):
        print(f"❌ [설정 파일 없음] {CONFIG_PATH}")
        print("   kis_info.yaml.example 을 복사해 kis_info.yaml 로 만들고 값을 채워주세요.")
        sys.exit(1)

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    required = ["app_key", "app_secret", "cano", "acnt_prdt_cd"]
    missing = [k for k in required if not cfg.get(k)]
    if missing:
        print(f"❌ [설정 누락] kis_info.yaml 에 다음 값이 필요합니다: {', '.join(missing)}")
        sys.exit(1)

    cfg.setdefault("is_real", True)
    return cfg


KIS_CONFIG = load_kis_config()
APP_KEY = KIS_CONFIG["app_key"]
APP_SECRET = KIS_CONFIG["app_secret"]
CANO = str(KIS_CONFIG["cano"])
ACNT_PRDT_CD = str(KIS_CONFIG["acnt_prdt_cd"])
IS_REAL = bool(KIS_CONFIG["is_real"])

BASE_URL = "https://openapi.koreainvestment.com:9443" if IS_REAL else "https://openapivts.koreainvestment.com:29443"

# TR_ID: 실전투자는 T로 시작, 모의투자는 V로 시작
TR_ID_ORDER_BUY = "TTTC0802U" if IS_REAL else "VTTC0802U"
TR_ID_ORDER_SELL = "TTTC0801U" if IS_REAL else "VTTC0801U"
TR_ID_BALANCE = "TTTC8434R" if IS_REAL else "VTTC8434R"
TR_ID_PRICE = "FHKST01010100"
TR_ID_DAILY_CHART = "FHKST03010100"
TR_ID_HOLIDAY = "CTCA0903R"


def parse_target_args(raw_args):
    """
    명령행 인자를 파싱하여 {종목코드: 배수} 딕셔너리로 반환
    예: '005930 1, 000660 10, 069500 2'
    """
    if not raw_args:
        return {"005930": 1}

    combined = " ".join(raw_args).replace(",", " ")
    tokens = combined.split()

    target_map = {}
    i = 0
    while i < len(tokens):
        symbol = tokens[i]
        multiplier = 1
        if i + 1 < len(tokens) and tokens[i + 1].isdigit():
            multiplier = int(tokens[i + 1])
            i += 1

        target_map[symbol] = max(1, multiplier)
        i += 1

    return target_map if target_map else {"005930": 1}


TARGET_STOCKS = parse_target_args(sys.argv[1:])

access_token = None
is_market_open_today = False
date_checked = None
decision_made = False
order_placed = False

stock_status = {
    symbol: {
        "prev_close": None,
        "yesterday_drop_rate": 0.0,
        "is_yesterday_down": False,
        "order_side": None,
        "order_qty": 0,
        "current_price": 0.0
    }
    for symbol in TARGET_STOCKS
}


# ─── [2. 기능 함수 정의부] ───

def adjust_price_to_tick_size(price, side="BUY"):
    """ 🎯 [호가 단위 조정] 계산된 주문 가격을 호가 단위(Tick Size)에 맞춤 """
    price = int(price)

    if price < 2000:
        tick = 1
    elif price < 5000:
        tick = 5
    elif price < 20000:
        tick = 10
    elif price < 50000:
        tick = 50
    elif price < 100000:
        tick = 100
    elif price < 500000:
        tick = 500
    else:
        tick = 1000

    adjusted = (price // tick) * tick

    # 호가 단위 미달 잔여금액 보정 (5원 단위 보정 등)
    if adjusted % 5 != 0:
        adjusted = (adjusted // 5) * 5

    return int(adjusted)


def fetch_access_token():
    """ [인증] KIS OAuth2 접근토큰(tokenP) 발급 """
    global access_token
    url = f"{BASE_URL}/oauth2/tokenP"
    payload = {
        "grant_type": "client_credentials",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET
    }
    try:
        response = requests.post(url, json=payload, timeout=5)
        if response.status_code == 200:
            access_token = response.json().get("access_token")
            return True
        print(f"❌ [토큰 발급 실패] HTTP Status: {response.status_code} | {response.text}")
        return False
    except Exception as e:
        print(f"❌ [토큰 발급 통신 예외] {e}")
        return False


def issue_hashkey(body):
    """ [해시키] 주문 바디 위변조 방지용 해시키 발급 """
    url = f"{BASE_URL}/uapi/hashkey"
    headers = {
        "Content-Type": "application/json",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET
    }
    try:
        response = requests.post(url, json=body, headers=headers, timeout=5)
        if response.status_code == 200:
            return response.json().get("HASH")
        print(f"⚠️ [해시키 발급 실패] HTTP {response.status_code}: {response.text}")
        return None
    except Exception as e:
        print(f"⚠️ [해시키 발급 예외] {e}")
        return None


def format_currency(value):
    """ 금액 콤마 포맷팅 """
    try:
        return format(int(float(value)), ",")
    except (ValueError, TypeError):
        return str(value)


def fetch_previous_close_price_for_symbol(symbol):
    """
    📊 [일봉 분석] "2일 연속 하락" 룰 판단을 위한 어제 자체의 등락 여부 산출

    KIS inquire-price는 오늘 대비 어제(전일대비율)만 제공하고 어제 대비 그제는 알려주지 않으므로,
    이 값만큼은 일봉(D-1, D-2 종가) 조회로 계산한다. 어제종가/오늘 변동률은
    get_multiple_stock_prices()가 inquire-price 응답값을 그대로 사용하므로 여기서 다시 쓰지 않는다.
    """
    global access_token, stock_status

    for attempt in range(2):
        if not access_token or attempt > 0:
            fetch_access_token()

        url = f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "appkey": APP_KEY,
            "appsecret": APP_SECRET,
            "tr_id": TR_ID_DAILY_CHART,
            "custtype": "P"
        }

        today = datetime.datetime.now()
        start_date = (today - datetime.timedelta(days=10)).strftime("%Y%m%d")
        end_date = today.strftime("%Y%m%d")

        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": symbol,
            "FID_INPUT_DATE_1": start_date,
            "FID_INPUT_DATE_2": end_date,
            "FID_PERIOD_DIV_CODE": "D",
            "FID_ORG_ADJ_PRC": "0"
        }

        try:
            response = requests.get(url, headers=headers, params=params, timeout=5)
            if response.status_code == 200:
                res_json = response.json()
                candles = res_json.get("output2", [])

                if isinstance(candles, list) and len(candles) > 0:
                    today_str = datetime.datetime.now().strftime("%Y%m%d")
                    past_candles = [c for c in candles if str(c.get("stck_bsop_date", "")) != today_str]
                    # 최신순 정렬 보장 (일자 내림차순)
                    past_candles.sort(key=lambda c: str(c.get("stck_bsop_date", "")), reverse=True)

                    if len(past_candles) >= 2:
                        yesterday_close = float(past_candles[0].get("stck_clpr", 0))
                        day_before_close = float(past_candles[1].get("stck_clpr", 0))

                        stock_status[symbol]["prev_close"] = yesterday_close

                        if yesterday_close < day_before_close and day_before_close > 0:
                            stock_status[symbol]["is_yesterday_down"] = True
                            stock_status[symbol]["yesterday_drop_rate"] = ((day_before_close - yesterday_close) / day_before_close) * 100
                            print(f"🎯 [{symbol}] 어제종가: {format_currency(yesterday_close)}원 |전일대비 하락(-{stock_status[symbol]['yesterday_drop_rate']:.2f}%)")
                        else:
                            stock_status[symbol]["is_yesterday_down"] = False
                            stock_status[symbol]["yesterday_drop_rate"] = 0.0
                            print(f"🎯 [{symbol}] 어제종가: {format_currency(yesterday_close)}원 |전일대비 상승")
                        return
                    elif len(past_candles) == 1:
                        stock_status[symbol]["prev_close"] = float(past_candles[0].get("stck_clpr", 0))
                        stock_status[symbol]["is_yesterday_down"] = False
                        stock_status[symbol]["yesterday_drop_rate"] = 0.0
                        return
            else:
                print(f"⚠️ [{symbol}] 일봉 조회 실패 (HTTP {response.status_code}): {response.text}")
        except Exception as e:
            print(f"⚠️ [{symbol}] 일봉 조회 시도 {attempt + 1}회 실패: {e}")

        time.sleep(0.5)

    print(f"⚠️ [{symbol}] 일봉 데이터 조회 실패로 기준가 미설정")
    stock_status[symbol]["prev_close"] = None


def fetch_all_previous_close_prices():
    """ 감시 리스트 전체 종목의 어제 종가 수신 """
    print("\n📊 [시세분석] 감시종목 어제종가 및 차트분석 시작...")
    for symbol in TARGET_STOCKS:
        fetch_previous_close_price_for_symbol(symbol)
        time.sleep(0.2)


def get_multiple_stock_prices():
    """
    🎯 감시 리스트 전체 종목의 실시간 시세 및 변동률 일괄 수신 (KIS는 단건 조회만 지원하여 순차 호출)

    KIS inquire-price 응답에 전일종가(stck_prdy_clpr)·전일대비율(prdy_ctrt)이 이미 포함되어 있어
    캔들로 직접 등락률을 계산할 필요 없이 API 값을 그대로 사용한다.
    """
    global access_token, stock_status
    if not access_token and not fetch_access_token():
        raise Exception("Access Token 발급 실패로 실시간 시세를 조회할 수 없습니다.")

    url = f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-price"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": TR_ID_PRICE,
        "custtype": "P"
    }

    result_map = {}
    for symbol in TARGET_STOCKS:
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": symbol
        }
        response = requests.get(url, headers=headers, params=params, timeout=5)
        if response.status_code != 200:
            raise Exception(f"[{symbol}] 시세 조회 API 실패 (HTTP {response.status_code}): {response.text}")

        res_json = response.json()
        output = res_json.get("output", {}) or {}

        last_price_raw = output.get("stck_prpr", "0")
        stock_name = output.get("hts_kor_isnm", symbol)
        prdy_ctrt_raw = output.get("prdy_ctrt", "0")
        prdy_clpr_raw = output.get("stck_prdy_clpr", "0")

        try:
            current_p = float(last_price_raw)
            stock_status[symbol]["current_price"] = current_p

            prdy_close = float(prdy_clpr_raw)
            if prdy_close > 0:
                stock_status[symbol]["prev_close"] = prdy_close

            change_rate_str = f"{float(prdy_ctrt_raw):.2f}"
        except ValueError:
            change_rate_str = "0.0"

        result_map[symbol] = {
            "price": str(last_price_raw),
            "rate": change_rate_str,
            "name": stock_name
        }
        time.sleep(0.1)

    return result_map


def calculate_expected_order(symbol, raw_change_str, current_price_raw):
    """ 종목별 변동률, 부여된 배수 및 2일 연속 하락 룰 기준 예상 주문 시뮬레이션 (수량 + 예상주문가) """
    global stock_status, TARGET_STOCKS
    multiplier = TARGET_STOCKS.get(symbol, 1)
    s_info = stock_status.get(symbol, {})

    try:
        raw_change = float(raw_change_str)
        curr_price = float(current_price_raw)

        if raw_change > 0:
            base_n = math.floor(abs(raw_change))
            n = base_n * multiplier
            target_price = adjust_price_to_tick_size(curr_price * 0.95, side="SELL")  # 매도: -5%
            return (f"매도(SELL) {n + 1}주 {format_currency(target_price)}원 (-5% 지정가)\n"
                    f"      └ [사유] 오늘 +{raw_change:.2f}% -> 기본n={base_n} * 배수({multiplier}) = n={n} -> n+1주")

        elif raw_change < 0:
            today_drop = abs(raw_change)
            is_y_down = s_info.get("is_yesterday_down", False)
            y_drop_rate = s_info.get("yesterday_drop_rate", 0.0)
            target_price = adjust_price_to_tick_size(curr_price * 1.05, side="BUY")  # 매수: +5%

            if is_y_down:
                total_drop = y_drop_rate + today_drop
                base_n = math.floor(total_drop)
                n = base_n * multiplier
                return (f"매수(BUY) {n + 2}주 {format_currency(target_price)}원 (+5% 지정가) [2일연속하락]\n"
                        f"      └ [사유] 어제-{y_drop_rate:.2f}% + 오늘-{today_drop:.2f}% = 총-{total_drop:.2f}% -> 기본n={base_n} * 배수({multiplier}) = n={n} -> n+2주")
            else:
                base_n = math.floor(today_drop)
                n = base_n * multiplier
                return (f"매수(BUY) {n + 2}주 {format_currency(target_price)}원 (+5% 지정가) [단독하락]\n"
                        f"      └ [사유] 오늘 -{today_drop:.2f}% -> 기본n={base_n} * 배수({multiplier}) = n={n} -> n+2주")
        else:
            return "매매 조건 미충족 (변동률 0%)"
    except Exception:
        return "계산 불가"


def display_asset_dashboard():
    """ 💰 [통합 대시보드] 전체 보유 계좌 및 멀티 감시 종목 상태 일괄 출력 """
    global access_token, stock_status, TARGET_STOCKS

    current_time_str = datetime.datetime.now().strftime("%H:%M")

    print(f"\n==================================================")
    print(f"⏰ [{current_time_str}] 통합 정기 대시보드 (총 {len(TARGET_STOCKS)}개 종목 감시)")
    print("==================================================")

    if not access_token and not fetch_access_token():
        print("❌ [오류] 토큰을 가져올 수 없어 대시보드 조회를 중단합니다.")
        print("==================================================\n")
        return

    try:
        url = f"{BASE_URL}/uapi/domestic-stock/v1/trading/inquire-balance"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "appkey": APP_KEY,
            "appsecret": APP_SECRET,
            "tr_id": TR_ID_BALANCE,
            "custtype": "P"
        }
        params = {
            "CANO": CANO,
            "ACNT_PRDT_CD": ACNT_PRDT_CD,
            "AFHR_FLPR_YN": "N",
            "OFL_YN": "",
            "INQR_DVSN": "02",
            "UNPR_DVSN": "01",
            "FUND_STTL_ICLD_YN": "N",
            "FNCG_AMT_AUTO_RDPT_YN": "N",
            "PRCS_DVSN": "01",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": ""
        }
        response = requests.get(url, headers=headers, params=params, timeout=5)

        if response.status_code != 200:
            print(f"❌ [API 오류] 잔고 조회 실패 (HTTP {response.status_code}): {response.text}")
            print("==================================================\n")
            return

        res_json = response.json()
        holdings_list = res_json.get("output1", []) or []
        summary_list = res_json.get("output2", []) or []
        summary = summary_list[0] if summary_list else {}

        stock_market_value = summary.get("scts_evlu_amt", 0)
        total_profit = summary.get("evlu_pfls_smtl_amt", 0)

        try:
            base_amt = float(stock_market_value) - float(total_profit)
            rate_formatted = f"{(float(total_profit) / base_amt * 100):+.2f}%" if base_amt != 0 else "0.00%"
        except (ValueError, TypeError):
            rate_formatted = "0.00%"

        holdings_map = {h.get("pdno"): h for h in holdings_list}

    except Exception as e:
        print(f"❌ [잔고 조회 예외] {e}")
        print("==================================================\n")
        return

    try:
        prices_map = get_multiple_stock_prices()
    except Exception as e:
        print(f"❌ [시세 일괄 조회 예외] {e}")
        print("==================================================\n")
        return

    print(f"💳 계좌 번호:    {CANO}-{ACNT_PRDT_CD}")
    print(f"📊 총 주식 평가액: {format_currency(stock_market_value)} 원")
    print(f"📈 총 주식 손익:   {format_currency(total_profit)} 원 ({rate_formatted})")
    print("--------------------------------------------------")
    print("📦 [계좌 보유주식 목록]")

    if holdings_list:
        for h in holdings_list:
            symbol = h.get("pdno", "")
            name = h.get("prdt_name", symbol)
            qty = h.get("hldg_qty", "0")

            pl_amount_raw = h.get("evlu_pfls_amt", "0")
            pl_rate_raw = h.get("evlu_pfls_rt", "0")

            try:
                rate_val = float(pl_rate_raw)
                rate_str = f"{rate_val:+.2f}%"
            except (ValueError, TypeError):
                rate_str = "+0.00%"

            amt_str = f"{format_currency(pl_amount_raw)}원"
            print(f" •{name}({symbol}): {qty}주 |손익 {amt_str}({rate_str})")
    else:
        print("  • 보유 중인 주식이 없습니다.")

    print("--------------------------------------------------")
    print("🎯 [자동매매 감시 종목별 실시간 상태]")

    for symbol, multiplier in TARGET_STOCKS.items():
        p_info = prices_map.get(symbol, {})
        target_price = p_info.get("price", "0")
        target_rate = p_info.get("rate", "0.0")
        stock_name = p_info.get("name", symbol)

        holding_item = holdings_map.get(symbol)
        if holding_item:
            avg_p = holding_item.get("pchs_avg_pric", "0")
            holding_qty = holding_item.get("hldg_qty", "0")
            avg_display = f"{format_currency(avg_p)}원 ({holding_qty}주 보유)"
        else:
            avg_display = "미보유"

        s_stat = stock_status.get(symbol, {})
        prev_close_display = format_currency(s_stat.get("prev_close")) if s_stat.get("prev_close") else "조회실패"
        y_status = f"어제 하락(-{s_stat.get('yesterday_drop_rate', 0.0):.2f}%)" if s_stat.get("is_yesterday_down") else "어제 상승/보합"

        expected_order = calculate_expected_order(symbol, target_rate, target_price)

        print(f"\n 📌 [{stock_name} / {symbol}] - 설정 배수: {multiplier}배")
        print(f"   •현재가: {format_currency(target_price)}원 (평단가: {avg_display}) | 어제종가: {prev_close_display}원 ({y_status})")
        print(f"   •당일 변동률: {target_rate}%")
        print(f"   •예상 주문: {expected_order}")

    print("==================================================\n")


def check_market_status():
    """
    [KIS OpenAPI] GET /uapi/domestic-stock/v1/quotations/chk-holiday

    반환값: True(개장) / False(주말 또는 확정된 휴장일) / None(API 오류로 판단 불가, 재시도 필요)
    """
    global access_token

    now = datetime.datetime.now()
    today_str = now.strftime("%Y-%m-%d")

    if now.weekday() >= 5:
        print(f"🔒 [{today_str}] 주말(토/일)로 인한 휴장일입니다.")
        return False

    if not access_token:
        fetch_access_token()

    url = f"{BASE_URL}/uapi/domestic-stock/v1/quotations/chk-holiday"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": TR_ID_HOLIDAY,
        "custtype": "P"
    }
    params = {
        "BASS_DT": now.strftime("%Y%m%d"),
        "CTX_AREA_NK": "",
        "CTX_AREA_FK": ""
    }

    try:
        response = requests.get(url, headers=headers, params=params, timeout=5)
        if response.status_code == 200:
            res_json = response.json()
            output = res_json.get("output", []) or []
            today_data = next((row for row in output if row.get("bass_dt") == now.strftime("%Y%m%d")), None)

            is_open = bool(today_data) and today_data.get("opnd_yn") == "Y"

            if is_open:
                print(f"🔓 [{today_str}] 국내 정규장 개장일입니다.")
                return True
            else:
                print(f"🔒 [{today_str}] 휴장일입니다. (opnd_yn: {today_data.get('opnd_yn') if today_data else 'N/A'})")
                return False

        print(f"⚠️ [KIS API] chk-holiday 응답 에러 (HTTP {response.status_code}): {response.text}")
        return None

    except Exception as e:
        print(f"⚠️ [KIS API] chk-holiday 통신 오류: {e}")
        return None


def send_kis_limit_order(stock_code, side, quantity, current_price):
    """ 🎯 [지정가 주문] 매수(+5%), 매도(-5%) 금액 계산 및 호가 단위 보정 후 KIS 현금주문(LIMIT) 전송 """
    global access_token

    if current_price <= 0:
        print(f"❌ [{stock_code} 주문 실패] 유효한 현재가 정보가 없습니다. ({current_price}원)")
        return

    if not access_token and not fetch_access_token():
        print(f"❌ [{stock_code} 주문 실패] Access Token 발급 실패")
        return

    # 지정가 계산 및 호가 단위 보정
    if side == "BUY":
        target_limit_price = adjust_price_to_tick_size(current_price * 1.05, side="BUY")
        side_label = "지정가 매수(+5%)"
        tr_id = TR_ID_ORDER_BUY
    else:  # SELL
        target_limit_price = adjust_price_to_tick_size(current_price * 0.95, side="SELL")
        side_label = "지정가 매도(-5%)"
        tr_id = TR_ID_ORDER_SELL

    body = {
        "CANO": CANO,
        "ACNT_PRDT_CD": ACNT_PRDT_CD,
        "PDNO": stock_code,
        "ORD_DVSN": "00",  # 00: 지정가
        "ORD_QTY": str(int(quantity)),
        "ORD_UNPR": str(int(target_limit_price))
    }

    hashkey = issue_hashkey(body)
    if not hashkey:
        print(f"❌ [{stock_code} 주문 실패] 해시키 발급 실패로 주문을 중단합니다.")
        return

    url = f"{BASE_URL}/uapi/domestic-stock/v1/trading/order-cash"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": tr_id,
        "custtype": "P",
        "hashkey": hashkey,
        "Content-Type": "application/json"
    }

    try:
        print(f"📡 [{stock_code} 지정가 주문 전송] 현재가: {format_currency(current_price)}원 -> 주문가: {format_currency(target_limit_price)}원 | 수량: {quantity}주 ({side_label})")
        response = requests.post(url, json=body, headers=headers, timeout=5)
        res_data = response.json() if response.content else {}

        if response.status_code == 200 and res_data.get("rt_cd") == "0":
            order_id = res_data.get("output", {}).get("ODNO", "알수없음")
            print(f"🚀 [KIS주문 성공] 종목: {stock_code} | {side} {quantity}주 {format_currency(target_limit_price)}원 체결요청 (주문번호: {order_id})")
        else:
            print(f"❌ [{stock_code} 주문실패] 상태 코드 ({response.status_code}): {res_data.get('msg1', response.text)}")
    except Exception as e:
        print(f"❌ [{stock_code} 주문 전송 오류] 시스템 예외 발생: {e}")


# ─── [3. 메인 실행 제어부] ───
if __name__ == "__main__":
    print("==================================================")
    print(f"🚀 [시스템 시작] 한국투자증권(KIS) 멀티종목 지정가 자동매매 ({'실전투자' if IS_REAL else '모의투자'})")
    print(f"📋 설정된 감시 종목 리스트 (총 {len(TARGET_STOCKS)}개):")
    for sym, mult in TARGET_STOCKS.items():
        print(f"   - 종목코드: {sym} | 설정 배수: {mult}배")
    print("📌 주문 방식: 지정가(LIMIT) [매수: 현재가 +5% | 매도: 현재가 -5%] (호가 단위 보정)")
    print("==================================================")

    if fetch_access_token():
        print("🔑 [인증성공] Access Token 발급 완료.")
        print(f"💳 [계좌확인] 계좌번호: {CANO}-{ACNT_PRDT_CD}")
        fetch_all_previous_close_prices()
        display_asset_dashboard()
    else:
        print("❌ [인증 실패] APP KEY 또는 APP SECRET을 확인하세요.")

    print("\n🤖 스케줄러 루프작동 (09:00~15:40 10분간격 자동조회)")

    last_dashboard_check = None

    while True:
        now = datetime.datetime.now()
        today_str = now.strftime("%Y-%m-%d")
        current_hm = (now.hour, now.minute)

        # ─── 매일 아침 개장 여부 및 데이터 초기화 ───
        if date_checked != today_str:
            if now.hour >= 8:
                print(f"\n🔍 [날짜 변경 감지] {today_str} 오늘 개장 여부 확인")
                market_result = check_market_status()

                if market_result is None:
                    print("⚠️ 개장 여부 확인 실패(API 오류). 30초 후 재시도합니다.")
                    time.sleep(30)
                    continue

                is_market_open_today = market_result
                if is_market_open_today:
                    print("🔓 오늘은 정규장 개장일. 자동매매 스케줄 가동")
                    fetch_all_previous_close_prices()
                else:
                    print("🔒 오늘은 휴장일(또는 주말)")
                date_checked = today_str

        if not is_market_open_today:
            print(f"💤 [휴식모드] 휴장일 1시간 대기(현재시각 {now.strftime('%H:%M:%S')})")
            time.sleep(3600)
            continue

        # ─── 09:00 ~ 15:40 10분 간격 자동 조회 ───
        if (9, 0) <= current_hm <= (15, 40):
            current_time_str = now.strftime("%H:%M")
            if now.minute % 10 == 0 and last_dashboard_check != current_time_str:
                display_asset_dashboard()
                last_dashboard_check = current_time_str

        # ─── [정규 규칙] 15:22 장마감 직전 전체 감시 종목 조건 체크 ───
        if now.hour == 15 and now.minute == 22 and not decision_made:
            print("\n⏰ [15:22] 장마감 직전 종목 변동률 체크 시작")

            try:
                prices_map = get_multiple_stock_prices()
            except Exception as e:
                print(f"⚠️ [15:22 오류] 시세 조회 실패 ({e}). 5초 후 재시도합니다.")
                time.sleep(5)
                continue

            for symbol, multiplier in TARGET_STOCKS.items():
                p_info = prices_map.get(symbol, {})
                target_rate_str = p_info.get("rate", "0.0")
                curr_price_val = float(p_info.get("price", 0))

                try:
                    raw_change = float(target_rate_str)
                except (ValueError, TypeError):
                    raw_change = 0.0

                s_info = stock_status[symbol]

                if raw_change > 0:
                    s_info["order_side"] = "SELL"
                    base_n = math.floor(abs(raw_change))
                    n = base_n * multiplier
                    s_info["order_qty"] = n + 1
                    target_limit_p = adjust_price_to_tick_size(curr_price_val * 0.95, side="SELL")
                    print(f"📈 [{symbol}] 상승감지 +{raw_change:.2f}% -> SELL {n+1}주 {format_currency(target_limit_p)}원(-5%)")

                elif raw_change < 0:
                    s_info["order_side"] = "BUY"
                    today_drop = abs(raw_change)
                    is_y_down = s_info.get("is_yesterday_down", False)
                    y_drop_rate = s_info.get("yesterday_drop_rate", 0.0)
                    target_limit_p = adjust_price_to_tick_size(curr_price_val * 1.05, side="BUY")

                    if is_y_down:
                        total_drop = y_drop_rate + today_drop
                        base_n = math.floor(total_drop)
                        n = base_n * multiplier
                        print(f"📉 [{symbol}] 이틀연속 하락 (어제 -{y_drop_rate:.2f}% + 오늘 -{today_drop:.2f}% = 합산 -{total_drop:.2f}%) -> BUY {n+2}주 {format_currency(target_limit_p)}원 (+5%)")
                    else:
                        base_n = math.floor(today_drop)
                        n = base_n * multiplier
                        print(f"📉 [{symbol}] 단독 하락 (-{today_drop:.2f}%) -> BUY {n+2}주 {format_currency(target_limit_p)}원 (+5%)")

                    s_info["order_qty"] = n + 2
                else:
                    s_info["order_side"] = None
                    s_info["order_qty"] = 0
                    print(f"📊 [{symbol}] 변동률 0% -> 매매 조건 미충족")

            decision_made = True

        # ─── [정규 규칙] 15:24 대상 종목 지정가 일괄 주문 전송 ───
        if now.hour == 15 and now.minute == 24 and not order_placed:
            print("\n⏳ [15:24] 예약된 종목별 지정가주문 순차적 전송...")
            executed_any = False

            try:
                prices_map = get_multiple_stock_prices()
            except Exception as e:
                print(f"⚠️ [15:24 시세갱신 실패] 이전 저장 시세를 사용하여 계산합니다: {e}")

            for symbol, s_info in stock_status.items():
                side = s_info.get("order_side")
                qty = s_info.get("order_qty", 0)
                curr_p = s_info.get("current_price", 0.0)

                if side and qty > 0:
                    executed_any = True
                    send_kis_limit_order(symbol, side, qty, curr_p)
                    time.sleep(0.5)

            if not executed_any:
                print("💤 [15:24] 오늘 실행 조건에 부합하는 주문이 없습니다.")

            order_placed = True

        # ─── 16:00 장 마감 후 플래그 초기화 ───
        if now.hour == 16 and now.minute == 0:
            decision_made = False
            order_placed = False
            last_dashboard_check = None
            access_token = None
            for s_info in stock_status.values():
                s_info["order_side"] = None
                s_info["order_qty"] = 0
            print("\n🔄 당일 장 정산 완료. 내부 상태 초기화, 내일 대기")
            time.sleep(60)

        time.sleep(1)
