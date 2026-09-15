import asyncio
import json
import re
from urllib.parse import parse_qsl

from crawlers._utils import new_http_client
from crawlers.hinacom import HinacomDownloader

_TARGET_URL = re.compile(r'var TARGET_URL = "([^"]+)"')
_BASE = "https://mdmis.cq12320.cn/wcs1/mdmis-app/h5"


async def run(share_url, *args, base_dir: str = "download"):
	query = dict(parse_qsl(share_url[share_url.rfind("?") + 1:]))
	client = new_http_client()
	try:
		print(f"下载海纳医信 DICOM（重庆卫健委），share_id: {query['share_id']}")

		# 入口页 https://mdmis.cq12320.cn/wcs1/mdmis-app/h5，拿 SF_cookie_15
		(await client.get(share_url)).close()

		# 拿 hospital_code 和 study_primary_id
		form = {
			"content": query["content"],
			"share_id": query["share_id"]
		}
		async with client.post(f"{_BASE}/api/share/check/time", json=form) as response:
			body = await response.json()
			if body["code"] != 200:
				raise Exception(body['message'])

			extend = json.loads(body["data"]["extend"])
			study, hospital = extend["study_primary_id"], extend["hospital_code"]

			print(f"hospital_code: {hospital}")
			print(f"study_primary_id: {study}\n")

		# 拿 ZFP_SessionId, ZFPXAUTH，注意这里自动重定向了一次：
		# /wcs1/mdmis-app/h5/api/qinming_h5/entry/study?token=...
		# CT 等大影像首次会返回"原始影像数据资料较大，请稍等待..."等待页，需轮询重试。
		viewer_url = None
		for attempt in range(1, 31):  # 最多 30 次 × 6 秒 ≈ 3 分钟
			async with client.get(f"{_BASE}/api/qinming_h5/api/ch/report/PacsEntry.aspx?hospitalCode={hospital}&studyPrimaryId={study}") as response:
				text = await response.text()
				matches = _TARGET_URL.search(text)
				if matches:
					viewer_url = str(response.real_url.origin()) + matches.group(1)
					break
			# 未拿到 TARGET_URL
			if "稍等待" in text or "原始影像数据资料较大" in text:
				print(f"影像数据准备中（第 {attempt} 次等待，6 秒后重试）...")
				await asyncio.sleep(6)
				continue
			# 既无 TARGET_URL 也非等待页，视为真错误
			raise RuntimeError(
				"PacsEntry.aspx 未返回查看器地址（var TARGET_URL），可能报告已过期或不可用。\n"
				f"响应内容前 500 字符: {text[:500]}"
			)
		if viewer_url is None:
			raise RuntimeError("等待影像数据准备超时（约 3 分钟），请稍后再试。")

		async with await HinacomDownloader.from_url(client, viewer_url) as downloader:
			return await downloader.download_all("--raw" in args, base_dir=base_dir)
	finally:
		await client.close()
