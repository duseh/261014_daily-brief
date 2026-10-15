#!/usr/bin/env python3
"""
update_brief.py
===============
매일 아침 국내 주요 경제 뉴스 RSS 및 시장 지표를 수집하고,
Gemini API(gemini-3-flash-preview -> gemini-3.8-flash -> gemini-2.5-flash)를 통해
구조화된 일간 인텔리전스 브리프(data.json)를 자동 생성하는 파이프라인 스크립트.
"""

import os
import sys
import json
import re
import time
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("BriefAutomation")

# 1. RSS 뉴스 피드 목록
RSS_FEEDS = [
    "https://www.mk.co.kr/rss/30100041/",      # 매일경제 경제
    "https://www.mk.co.kr/rss/50200011/",      # 매일경제 증권
    "https://www.hankyung.com/feed/economy",    # 한국경제 경제
    "https://www.hankyung.com/feed/finance",    # 한국경제 증권/금융
]

# 기본 폴백 시장 지표
DEFAULT_INDICATORS = [
    {"name": "KOSPI", "value": "2,600.00", "change": "0.00", "change_rate": "0.00%", "direction": "FLAT"},
    {"name": "KOSDAQ", "value": "850.00", "change": "0.00", "change_rate": "0.00%", "direction": "FLAT"},
    {"name": "USD/KRW", "value": "1,350.00", "change": "0.00", "change_rate": "0.00%", "direction": "FLAT"}
]


def clean_html(raw_html: str) -> str:
    """HTML 태그 및 불필요한 공백을 제거하고 순수 텍스트만 반환합니다."""
    if not raw_html:
        return ""
    clean_text = re.sub(r"<[^>]+>", " ", raw_html)
    clean_text = re.sub(r"&[a-zA-Z0-9#]+;", " ", clean_text)
    clean_text = re.sub(r"\s+", " ", clean_text).strip()
    return clean_text


def fetch_rss_news(limit: int = 15) -> List[Dict[str, str]]:
    """국내 주요 경제 뉴스 RSS 피드에서 최신 기사 10~15건의 제목 및 요약을 수집합니다."""
    articles = []
    seen_titles = set()
    custom_headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    }

    # feedparser 임포트 시도, 미설치 시 내장 urllib/xml 폴백
    try:
        import feedparser
        has_feedparser = True
    except ImportError:
        has_feedparser = False
        logger.warning("feedparser 라이브러리가 없습니다. urllib와 xml.etree를 사용합니다.")

    for feed_url in RSS_FEEDS:
        try:
            logger.info(f"RSS 피드 수집 시도: {feed_url}")
            if has_feedparser:
                feed = feedparser.parse(feed_url, request_headers=custom_headers)
                entries = feed.entries
                for entry in entries:
                    title = clean_html(getattr(entry, "title", ""))
                    summary = clean_html(getattr(entry, "summary", getattr(entry, "description", "")))
                    link = getattr(entry, "link", "")

                    if title and title not in seen_titles:
                        seen_titles.add(title)
                        articles.append({
                            "title": title,
                            "summary": summary[:300],
                            "link": link
                        })
            else:
                import urllib.request
                import xml.etree.ElementTree as ET

                req = urllib.request.Request(feed_url, headers=custom_headers)
                with urllib.request.urlopen(req, timeout=10) as resp:
                    tree = ET.parse(resp)
                    root = tree.getroot()
                    for item in root.findall(".//item"):
                        title = clean_html(item.findtext("title", ""))
                        summary = clean_html(item.findtext("description", ""))
                        link = item.findtext("link", "")

                        if title and title not in seen_titles:
                            seen_titles.add(title)
                            articles.append({
                                "title": title,
                                "summary": summary[:300],
                                "link": link
                            })

            if len(articles) >= limit:
                break
        except Exception as e:
            logger.warning(f"RSS 피드 수집 중 오류 ({feed_url}): {e}")

    logger.info(f"총 {len(articles[:limit])}건의 최신 경제 뉴스 기사 수집 완료")
    return articles[:limit]


def fetch_market_indicators() -> List[Dict[str, str]]:
    """
    네이버 금융 웹페이지/API를 크롤링하여 KOSPI, KOSDAQ, USD/KRW 지표를 수집합니다.
    실패 시 기존 data.json의 지표를 재사용하거나 기본 폴백 지표를 반환합니다.
    """
    try:
        import requests
        from bs4 import BeautifulSoup

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }

        # 1. 네이버 금융 메인 페이지 파싱 (KOSPI, KOSDAQ)
        sise_url = "https://finance.naver.com/sise/"
        res = requests.get(sise_url, headers=headers, timeout=8)
        soup = BeautifulSoup(res.content.decode("euc-kr", "replace"), "html.parser")

        def extract_sise(idx_id: str, change_id: str, name: str) -> Dict[str, str]:
            now_el = soup.find(id=idx_id)
            change_el = soup.find(id=change_id)
            if not now_el:
                raise ValueError(f"{name} 요소를 찾을 수 없습니다.")

            value = now_el.get_text(strip=True)
            change_text = change_el.get_text(strip=True) if change_el else ""

            # 변동값 및 비율 분리 (예: "▲ 47.55 +0.82%")
            direction = "FLAT"
            if "▲" in change_text or "+" in change_text:
                direction = "UP"
            elif "▼" in change_text or "-" in change_text:
                direction = "DOWN"

            nums = re.findall(r"[-+]?\d*\.?\d+", change_text.replace(",", ""))
            change = nums[0] if len(nums) > 0 else "0.00"
            change_rate = (nums[1] + "%") if len(nums) > 1 else "0.00%"

            if direction == "UP" and not change.startswith("+"):
                change = "+" + change
                change_rate = "+" + change_rate
            elif direction == "DOWN" and not change.startswith("-"):
                change = "-" + change
                change_rate = "-" + change_rate

            return {
                "name": name,
                "value": value,
                "change": change,
                "change_rate": change_rate,
                "direction": direction
            }

        kospi = extract_sise("KOSPI_now", "KOSPI_change", "KOSPI")
        kosdaq = extract_sise("KOSDAQ_now", "KOSDAQ_change", "KOSDAQ")

        # 2. 환율 파싱 (USD/KRW)
        fx_url = "https://finance.naver.com/marketindex/"
        fx_res = requests.get(fx_url, headers=headers, timeout=8)
        fx_soup = BeautifulSoup(fx_res.content.decode("euc-kr", "replace"), "html.parser")

        fx_val_el = fx_soup.select_one(".head_usd + .point, #exchangeList .value")
        fx_change_el = fx_soup.select_one(".head_usd + .point + .change, #exchangeList .change")
        fx_blind_el = fx_soup.select_one(".head_usd + .point + .change + .blind, #exchangeList .blind")

        if fx_val_el:
            usd_val = fx_val_el.get_text(strip=True)
            usd_change_raw = fx_change_el.get_text(strip=True) if fx_change_el else "0.00"
            usd_blind = fx_blind_el.get_text(strip=True) if fx_blind_el else ""

            fx_dir = "FLAT"
            if "상승" in usd_blind or "+" in usd_change_raw:
                fx_dir = "UP"
            elif "하락" in usd_blind or "-" in usd_change_raw:
                fx_dir = "DOWN"

            usd_change_clean = re.sub(r"[^\d.]", "", usd_change_raw)
            try:
                base_num = float(usd_val.replace(",", ""))
                chg_num = float(usd_change_clean)
                rate_val = round((chg_num / (base_num - chg_num if fx_dir == "UP" else base_num + chg_num)) * 100, 2)
            except Exception:
                rate_val = 0.0

            if fx_dir == "UP":
                usd_change = "+" + usd_change_clean
                usd_rate = f"+{rate_val}%"
            elif fx_dir == "DOWN":
                usd_change = "-" + usd_change_clean
                usd_rate = f"-{rate_val}%"
            else:
                usd_change = usd_change_clean
                usd_rate = f"{rate_val}%"

            usd = {
                "name": "USD/KRW",
                "value": usd_val,
                "change": usd_change,
                "change_rate": usd_rate,
                "direction": fx_dir
            }
        else:
            usd = DEFAULT_INDICATORS[2]

        indicators = [kospi, kosdaq, usd]
        logger.info(f"실시간 시장 지표 수집 성공: {indicators}")
        return indicators

    except Exception as e:
        logger.warning(f"시장 지표 실시간 크롤링 실패, 폴백 데이터를 사용합니다: {e}")
        # 기존 data.json이 있으면 거기서 재사용
        if os.path.exists("data.json"):
            try:
                with open("data.json", "r", encoding="utf-8") as f:
                    prev = json.load(f)
                    if "market_indicators" in prev and len(prev["market_indicators"]) == 3:
                        logger.info("기존 data.json의 시장 지표를 계승합니다.")
                        return prev["market_indicators"]
            except Exception:
                pass
        return DEFAULT_INDICATORS


def generate_brief_with_gemini(
    articles: List[Dict[str, str]],
    indicators: List[Dict[str, str]],
    today_str: str,
    max_retries_per_model: int = 2
) -> Dict[str, Any]:
    """Gemini API를 호출하여 data.json 스키마에 일치하는 JSON 브리프 데이터를 생성합니다.
    503(과부하) 또는 404(모델 미지원) 발생 시 다음 순서로 모델을 자동 폴백(Fallback)합니다:
    ['gemini-3-flash-preview', 'gemini-3.8-flash', 'gemini-2.5-flash']"""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("환경 변수 GEMINI_API_KEY가 설정되어 있지 않습니다.")

    # 프롬프트 구성
    articles_context = "\n".join([
        f"- [기사 {i+1}] {item['title']}: {item['summary']}"
        for i, item in enumerate(articles)
    ])

    indicators_context = json.dumps(indicators, ensure_ascii=False, indent=2)

    system_instruction = (
        "당신은 글로벌 투자은행(IB)의 수석 거시경제 및 반도체/테크 산업 수석 애널리스트입니다.\n"
        "제공된 최신 경제 기사들과 시장 지표를 분석하여 기관 투자자를 위한 '일간 경제 & 반도체 인텔리전스 리포트'를 작성하십시오.\n"
        "반드시 JSON 형식으로만 응답해야 하며, 지정된 스키마를 엄격히 준수하십시오.\n"
        "분석은 피상적인 뉴스 나열이 아닌, 구조적 인과관계(관측 팩트 -> 파급 경로 -> 반증 조건 -> 인사이트 팁)를 깊이 있게 다루어야 합니다."
    )

    prompt = f"""
오늘 날짜: {today_str}

[오늘의 시장 지표]
{indicators_context}

[오늘 수집된 최신 주요 경제/산업 기사 목록]
{articles_context}

위 뉴스들을 바탕으로 아래 JSON 스키마 규격에 맞추어 정확한 JSON 객체를 생성하십시오.

[반드시 준수해야 할 JSON 스키마 및 가이드]
1. `date`: "{today_str}"
2. `macro_key_points`: 오늘의 가장 중요한 거시경제/산업 동향 핵심 요약 3문장 (문자열 배열 3개)
3. `market_indicators`: 전달된 시장 지표 목록을 그대로 포함
4. `top_news`: 가장 영향력이 큰 5개의 핵심 이슈 심층 분석 카드 (정확히 5개 객체의 배열)
   각 객체는 다음 필드를 필수로 가집니다:
   - `id`: 1부터 5까지의 정수
   - `title`: 명확하고 전문적인 분석형 헤드라인 제목 (한국어)
   - `observed_facts`: 뉴스에서 확인된 구체적 팩트 및 수치 데이터 (문자열 배열 2~3개)
   - `transmission_channels`: 이 사실이 시장, 산업 밸류체인 및 기업 실적에 미치는 구체적 파급 경로 (문자열 배열 2개)
   - `refutation_conditions`: '이 분석이 틀렸다면' - 분석의 전제가 뒤집히는 리스크 조건이나 반증 트리거 (문자열 배열 2개)
   - `tip`: 투자자 및 의사결정자를 위한 실전 모니터링 인사이트 조언 (1~2문장의 문자열)

순수 JSON 데이터만 출력하십시오.
"""

    candidate_models = [
        "gemini-3-flash-preview",
        "gemini-3.8-flash",
        "gemini-2.5-flash"
    ]

    last_exception = None

    for model_index, model_name in enumerate(candidate_models, 1):
        logger.info(f"[{model_index}/{len(candidate_models)}] 대상 모델 시도: '{model_name}'")

        for attempt in range(1, max_retries_per_model + 1):
            try:
                logger.info(f"Gemini API 호출 중... (시도 {attempt}/{max_retries_per_model}) [모델: {model_name}]")

                # 1. 최신 google-genai SDK 시도
                try:
                    from google import genai
                    from google.genai import types

                    client = genai.Client(api_key=api_key)
                    response = client.models.generate_content(
                        model=model_name,
                        contents=prompt,
                        config=types.GenerateContentConfig(
                            system_instruction=system_instruction,
                            response_mime_type="application/json",
                            temperature=0.2,
                        )
                    )
                    content_text = response.text
                except ImportError:
                    # 2. 레거시 google-generativeai SDK 폴백
                    import google.generativeai as legacy_genai

                    legacy_genai.configure(api_key=api_key)
                    model = legacy_genai.GenerativeModel(
                        model_name=model_name,
                        system_instruction=system_instruction,
                        generation_config={"response_mime_type": "application/json", "temperature": 0.2}
                    )
                    response = model.generate_content(prompt)
                    content_text = response.text

                # JSON 마크다운 백틱 제거 방어
                cleaned_text = re.sub(r"^```(?:json)?\s*", "", content_text.strip(), flags=re.MULTILINE)
                cleaned_text = re.sub(r"\s*```$", "", cleaned_text.strip(), flags=re.MULTILINE)

                data = json.loads(cleaned_text)

                # 기본 스키마 무결성 검증
                assert "date" in data, "date 필드가 누락되었습니다."
                assert "macro_key_points" in data and len(data["macro_key_points"]) >= 1, "macro_key_points가 누락되었습니다."
                assert "market_indicators" in data, "market_indicators가 누락되었습니다."
                assert "top_news" in data and len(data["top_news"]) >= 1, "top_news가 누락되었습니다."

                logger.info(f"Gemini API 호출 및 브리프 데이터 생성 성공! (최종 사용 모델: {model_name})")
                return data

            except Exception as e:
                last_exception = e
                err_str = str(e).lower()
                logger.warning(f"모델 '{model_name}' 호출 실패 (시도 {attempt}/{max_retries_per_model}): {e}")

                # 404 모델 없음 또는 미지원인 경우 즉시 다음 폴백 모델로 이동
                if "404" in err_str or "not found" in err_str:
                    logger.warning(f"모델 '{model_name}'을 찾을 수 없거나 미지원(404) 상태입니다. 다음 폴백 모델로 즉시 전환합니다.")
                    break

                # 503(과부하) 또는 기타 일시적 오류인 경우 재시도 대기
                if attempt < max_retries_per_model:
                    wait_sec = attempt * 3
                    logger.info(f"서버 과부하(503) 또는 일시적 오류 감지. {wait_sec}초 대기 후 재시도합니다...")
                    time.sleep(wait_sec)
                else:
                    logger.warning(f"모델 '{model_name}' 재시도 한도 도달. 다음 폴백 모델을 시도합니다.")

    logger.error("지정된 모든 후보 모델(gemini-3-flash-preview, gemini-3.8-flash, gemini-2.5-flash) 호출에 실패했습니다.")
    raise last_exception


def main():
    # 한국 표준시(KST, UTC+9) 기준 당일 날짜 계산
    kst = timezone(timedelta(hours=9))
    now_kst = datetime.now(kst)
    today_str = now_kst.strftime("%Y-%m-%d")
    logger.info(f"데일리 브리프 자동 생성 파이프라인 시작 (기준일: {today_str})")

    # 1. RSS 뉴스 수집
    articles = fetch_rss_news(limit=15)
    if not articles:
        logger.warning("RSS 기사를 수집하지 못했습니다. 기본 템플릿 기사를 사용합니다.")
        articles = [
            {"title": "반도체 및 거시경제 지표 안정세 지속", "summary": "금리 안정과 기술주 중심 저가 매수세 유입으로 지수 방어 흐름", "link": ""}
        ]

    # 2. 시장 지표 수집
    indicators = fetch_market_indicators()

    # 3. Gemini API 호출 및 JSON 생성
    try:
        brief_data = generate_brief_with_gemini(articles, indicators, today_str)
    except Exception as e:
        logger.error(f"Gemini API 호출 및 브리프 생성 실패: {e}")
        sys.exit(1)

    # 4. data.json 파일 저장
    output_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(brief_data, f, ensure_ascii=False, indent=2)

    logger.info(f"성공적으로 {output_path} 파일이 갱신되었습니다. (총 뉴스 수: {len(brief_data.get('top_news', []))}개)")


if __name__ == "__main__":
    main()
