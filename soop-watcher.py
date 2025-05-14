import asyncio
import ssl
import certifi
import websockets
import requests
import re
import aiohttp
from collections import defaultdict
from playwright.async_api import async_playwright
from datetime import datetime
from asyncio import TimeoutError, wait_for

API_URL = "https://sch.sooplive.co.kr/api.php"
CATEGORY_NO = "00040001"  # 스타크래프트 카테고리 번호
CATEGORY_BASE_URL = "https://play.sooplive.co.kr"

DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/1362448207245742143/IWkTnvokQFGlZnqJPgVWjfoKzxDNJu1fQletr6uRxZLUnWd_bof1blNt_VleqD6H0-hc"
CATEGORY_URL = "https://www.sooplive.co.kr/directory/category/%EC%8A%A4%ED%83%80%ED%81%AC%EB%9E%98%ED%94%84%ED%8A%B8/live"

F = "\x0c"
ESC = "\x1b\t"

PREV_WATCHING = defaultdict(set)     # {uid: set of stream_ids}
CURRENT_WATCHING = defaultdict(set)  # {uid: set of stream_ids}
NICKNAMES = dict()  # uid → 닉네임

def load_target_ids(filename="targets.txt"):
    try:
        with open(filename, "r", encoding="utf-8") as f:
            return {line.strip().lower() for line in f if line.strip()}
    except FileNotFoundError:
        print(f"{now()} ⚠️ 감시 대상 파일 {filename}을 찾을 수 없습니다.")
        return set()

def now():
    return datetime.now().strftime("[%H:%M:%S]")

def log(bid, msg):
    print(f"{now()}[{bid:>13}] {msg}")

def get_player_live(bno, bid):
    url = 'https://live.sooplive.co.kr/afreeca/player_live_api.php'
    data = {
        'bid': bid,
        'bno': bno,
        'type': 'live',
        'confirm_adult': 'false',
        'player_type': 'html5',
        'mode': 'landing',
        'from_api': '0',
        'pwd': '',
        'stream_type': 'common',
        'quality': 'HD'
    }
    try:
        response = requests.post(f'{url}?bjid={bid}', data=data)
        response.raise_for_status()
        res = response.json()
        chdomain = res["CHANNEL"]["CHDOMAIN"].lower()
        chatno = res["CHANNEL"]["CHATNO"]
        ftk = res["CHANNEL"]["FTK"]
        chpt = str(int(res["CHANNEL"]["CHPT"]) + 1)
        return chdomain, chatno, ftk, chpt, res["CHANNEL"]["TITLE"]
    except Exception as e:
        log(bid, f"[API ERROR] 요청 실패 - {e}")
        return None, None, None, None, None

def make_packets(chatno):
    connect_packet = f'{ESC}000100000600{F*3}16{F}'
    join_packet = f'{ESC}0002{len(chatno)+6:06}00{F}{chatno}{F*5}'
    ping_packet = f'{ESC}000000000100{F}'
    return connect_packet, join_packet, ping_packet

async def send_discord_alert(matched_info, title, bid, bno):
    stream_url = f"https://play.sooplive.co.kr/{bid}/{bno}"
    user_info = ", ".join([f"**{nick}({uid})**" for uid, nick in matched_info])
    message = f"🔔 {user_info} 님이 [**{title}**]({stream_url}) 방송을 보고 있습니다."
    log(bid, f"{message}")
    async with aiohttp.ClientSession() as session:
        await session.post(DISCORD_WEBHOOK_URL, json={"content": message})

async def send_discord(message):
    async with aiohttp.ClientSession() as session:
        await session.post(DISCORD_WEBHOOK_URL, json={"content": message})


async def extract_starcraft_streams():
    print(f"{now()} 📡 스타크래프트 카테고리 API 호출 중...")
    results = []
    page = 1

    async with aiohttp.ClientSession() as session:
        while True:
            params = {
                "m": "categoryContentsList",
                "szType": "live",
                "nPageNo": page,
                "nListCnt": 60,
                "szPlatform": "pc",
                "szOrder": "view_cnt_desc",
                "szCateNo": CATEGORY_NO
            }

            try:
                async with session.get(API_URL, params=params) as response:
                    response.raise_for_status()
                    json_data = await response.json()

                    data = json_data.get("data", {})
                    streams = data.get("list", [])
                    if not streams:
                        break

                    for stream in streams:
                        bid = stream.get("user_id")
                        bno = stream.get("broad_no")
                        if bid and bno:
                            url = f"{CATEGORY_BASE_URL}/{bid}/{bno}"
                            results.append({
                                "bid": bid,
                                "bno": str(bno),
                                "url": url,
                                "title": stream.get("broad_title", ""),
                                "view_cnt": stream.get("view_cnt", 0)
                            })

                    if not data.get("is_more"):
                        break
                    page += 1

            except Exception as e:
                print(f"{now()} [!] API 요청 실패 (페이지 {page}): {e}")
                break

    print(f"{now()} 📦 총 {len(results)}개의 방송 수집 완료")
    return results

async def watch_stream(index, total, bid, bno):
    log(bid, f"방송 연결 시도 중 📺[{index}/{total}]")
    start_time = datetime.now()

    chdomain, chatno, ftk, chpt, stream_title = get_player_live(bno, bid)
    if not chdomain:
        log(bid, f"[!] 방송 정보 스킵됨")
        return

    uri = f"wss://{chdomain}:{chpt}/Websocket/{bid}"
    connect_packet, join_packet, ping_packet = make_packets(chatno)

    ssl_context = ssl.create_default_context(cafile=certifi.where())
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE

    seen_viewers = defaultdict(str)
    try:
        async with websockets.connect(uri, subprotocols=['chat'], ssl=ssl_context, ping_interval=None) as websocket:
            await websocket.send(connect_packet)
            await asyncio.sleep(1)
            await websocket.send(join_packet)
            log(bid, f"WebSocket 연결됨: ✅")

            stop_event = asyncio.Event()

            async def ping():
                while not stop_event.is_set():
                    await asyncio.sleep(60)
                    await websocket.send(ping_packet)

            async def receive():
                while True:
                    try:
                        # ⏱️ 10초 안에 패킷이 안 오면 asyncio.TimeoutError 발생
                        msg = await asyncio.wait_for(websocket.recv(), timeout=6)
                    except asyncio.TimeoutError:
                        log(bid, f"⏰ 10초 동안 패킷 없음 → 타임아웃 종료")
                        await websocket.close()
                        stop_event.set()
                        break
                    except websockets.exceptions.ConnectionClosed:
                        log(bid, f"❌ WebSocket 연결 종료됨")
                        break
                    except Exception as e:
                        log(bid, f"❗ 수신 오류: {type(e).__name__}: {e}")
                        break

                    if isinstance(msg, bytes):
                        text = msg.decode('utf-8', errors='ignore')

                        if text.startswith(f'{ESC}0004'):
                            parts = text.split(F)

                            packet_viewer_count = 0
                            new_count = 0

                            for i in range(2, len(parts) - 2, 3):
                                try:
                                    nickname = parts[i]
                                    nickname_cleaned = re.sub(r"\(\d+\)$", "", nickname)
                                    display_name = parts[i+1]
                                    info = parts[i+2]

                                    packet_viewer_count += 1

                                    if nickname_cleaned not in seen_viewers:
                                        seen_viewers[nickname_cleaned] = f"{display_name} | {info}"
                                        new_count += 1
                                except IndexError:
                                    continue

                            log(bid, f"📥 시청자 패킷 수신 – 현재 패킷: {packet_viewer_count}명 (신규 {new_count}명) (총 {len(seen_viewers)}명)")

                            # 40명 미만이면 종료
                            if packet_viewer_count < 40:
                                log(bid, f"✅ 패킷 인원이 40명 미만이므로 감시 종료")
                                await websocket.close()
                                stop_event.set()
                                break

            ping_task = asyncio.create_task(ping())
            await receive()
            ping_task.cancel()
            await asyncio.sleep(0.5)

        filtered_target_ids = {tid.lower() for tid in TARGET_IDS if tid.lower() != bid.lower()}
        if not filtered_target_ids:
            log(bid, f"현재 방송은 감시 대상이지만 비교할 다른 대상이 없으므로 스킵됩니다. 🙅‍♂️")
            return

        matched = [uid for uid in seen_viewers if uid in filtered_target_ids]
        if matched:
            matched_info = [(uid, seen_viewers[uid].split('|')[0].strip()) for uid in matched]
            #await send_discord_alert(matched_info, stream_title, bid, bno)
        else:
            log(bid, f"감시 대상 없음. 다음 방송으로 이동합니다. ❌")

        for uid in filtered_target_ids:
            if uid in seen_viewers:
                NICKNAMES[uid] = seen_viewers[uid].split('|')[0].strip()
                CURRENT_WATCHING[uid].add(f"{bid}/{bno}")
    
    except Exception as e:
        log(bid, f"[!] WebSocket 실패 : {type(e).__name__}: {e}")
    finally:
        duration = (datetime.now() - start_time).total_seconds()
        log(bid, f"감시 시간: {duration:.1f}초 ⏱️ ")

async def limited_watch(streams, limit=10):
    sem = asyncio.Semaphore(limit)

    async def wrapper(index, stream):
        async with sem:
            await watch_stream(index + 1, len(streams), stream['bid'], stream['bno'])

    tasks = [asyncio.create_task(wrapper(i, stream)) for i, stream in enumerate(streams)]
    await asyncio.gather(*tasks)

async def batch_watch(streams, batch_size=20, limit=10):
    total = len(streams)
    total_batches = (total + batch_size - 1) // batch_size

    for batch_idx, i in enumerate(range(0, total, batch_size), 1):
        batch = streams[i:i+batch_size]
        print(f"{now()} 🚀 감시 배치 {batch_idx}/{total_batches} 시작: 방송 {i+1}~{i+len(batch)}")

        await limited_watch(batch, limit=limit)

        if batch_idx < total_batches:
            print(f"{now()} ⏸️ 다음 배치까지 잠시 대기...")
            await asyncio.sleep(1)  # 각 배치 간 약간의 딜레이

async def compare_and_alert_watch_changes(streams):
    global PREV_WATCHING
    stream_title_map = {f"{s['bid']}/{s['bno']}": s["title"] for s in streams}
    stream_url_map = {f"{s['bid']}/{s['bno']}": f"https://play.sooplive.co.kr/{s['bid']}/{s['bno']}" for s in streams}

    broadcast_events = defaultdict(list)
    anyone_watching_now = False

    for uid in TARGET_IDS:
        prev = PREV_WATCHING[uid]
        curr = CURRENT_WATCHING[uid]
        started = curr - prev
        stopped = prev - curr

        for sid in started:
            broadcast_events[sid].append((uid, "started"))

        for sid in stopped:
            broadcast_events[sid].append((uid, "stopped"))

        if curr:
            anyone_watching_now = True

        PREV_WATCHING[uid] = curr.copy()

    for sid, events in broadcast_events.items():
        bid, bno = sid.split("/")
        title = stream_title_map.get(sid, "(제목 없음)")
        url = stream_url_map.get(sid, f"https://play.sooplive.co.kr/{bid}/{bno}")

        started_users = [uid for uid, action in events if action == "started"]
        stopped_users = [uid for uid, action in events if action == "stopped"]

        message = f"📺 **[{title}]({url})** 방송 상태 변경\n"
        if started_users:
            message += "🔔 시청 시작: " + ", ".join(f"{NICKNAMES.get(uid, '닉네임')}({uid})" for uid in started_users) + "\n"
        if stopped_users:
            message += "🔕 시청 종료: " + ", ".join(f"{NICKNAMES.get(uid, '닉네임')}({uid})" for uid in stopped_users)

        await send_discord(message)

    if not anyone_watching_now and any(PREV_WATCHING[uid] for uid in TARGET_IDS):
        await send_discord("📴 감시 대상들이 현재 어떤 방송도 시청하지 않고 있습니다.")


async def main():
    global TARGET_IDS, CURRENT_WATCHING
    CURRENT_WATCHING = defaultdict(set) 
    TARGET_IDS = load_target_ids()  # 외부에서 로드
    if not TARGET_IDS:
        print(f"{now()} 감시 대상이 없습니다. 종료합니다.")
        return

    streams = await extract_starcraft_streams()
    print(f"{now()} 📺 감시 대상 방송 수: {len(streams)}")
    await batch_watch(streams, batch_size=20, limit=10)
    await compare_and_alert_watch_changes(streams)

async def main_loop():
    while True:
        await main()
        await asyncio.sleep(60)  # 5분 간격 반복

if __name__ == "__main__":
    asyncio.run(main_loop())
