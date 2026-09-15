"""
下载海纳医信 miShare（*.medicalimagecloud.com） 上面的云影像，爬虫流程见：
https://blog.kaciras.com/article/45/download-dicom-files-from-hinacom-cloud-viewer
"""
import asyncio
import re
import sys
from pathlib import Path
from typing import Any

from aiohttp import ClientSession
from pydicom.datadict import DicomDictionary
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.encaps import encapsulate
from pydicom.tag import Tag
from pydicom.uid import ExplicitVRLittleEndian, JPEG2000Lossless
from tqdm import tqdm

from crawlers._utils import pathify, parse_dcm_value, SeriesDirectory, suggest_save_dir

_LINK_ENTRY = re.compile(r"window\.location\.href = '([^']+)'")
_TARGET_PATH = re.compile(r'var TARGET_PATH = "([^"]+)"')
_VAR_RE = re.compile(r'var (STUDY_ID|ACCESSION_NUMBER|STUDY_EXAM_UID|LOAD_IMAGE_CACHE_KEY) = "([^"]*)"')


def _get_save_dir(ds, base_dir: str = "download"):
	return suggest_save_dir(ds["patientName"], ds["studyDescription"], ds["studyDate"], base_dir)


class HinacomDownloader:
	"""
	海纳医信医疗影像系统的下载器，该系统在中国的多个地区被采用。
	"""

	client: ClientSession
	cache_key: str
	dataset: dict[str, Any]

	def __init__(self, client, cache_key, dataset):
		self.client = client
		self.cache_key = cache_key
		self.dataset = dataset
		self.refreshing = asyncio.create_task(self._refresh_cac())

	async def __aenter__(self):
		return self

	def __aexit__(self, *ignore):
		self.refreshing.cancel()
		return self.client.close()

	async def _refresh_cac(self):
		"""每分钟要刷新一下 CAC_AUTH 令牌，因为 PY 没有尾递归优化所以还是用循环"""
		while True:
			await asyncio.sleep(60)
			(await self.client.get("ImageViewer/renewcacauth")).close()

	async def get_tags(self, info):
		api = "ImageViewer/GetImageDicomTags"
		params = {
			"studyId": info['studyId'],
			"imageId": info['imageId'],
			"frame": "0",
			"storageNodes": self.dataset["storageNode"] or "",
		}
		async with self.client.get(api, params=params) as response:
			return await response.json()

	async def get_image(self, info, raw: bool):
		s, i = info['studyId'], info['imageId'],
		if raw:
			api = f"imageservice/api/image/dicom/{s}/{i}/0/0"
		else:
			api = f"imageservice/api/image/j2k/{s}/{i}/0/3"

		params = {
			"storageNodes": self.dataset["storageNode"] or "",
			"ck": self.cache_key,
		}
		async with self.client.get(api, params=params) as response:
			return await response.read(), response.headers["X-ImageFrame"]

	async def download_all(self, is_raw=False, base_dir: str = "download"):
		"""
		快捷方法，下载全部序列到 DCM 文件，保存的文件名将根据报告自动生成。
		该方法会在控制台显示进度条和相关信息。

		:param is_raw: 是否下载未压缩的图像，默认下载 JPEG2000 格式的。
		:param base_dir: 保存的根目录，默认 download
		"""
		save_to = _get_save_dir(self.dataset, base_dir)
		print(f'保存到: {save_to}')

		for series in self.dataset["displaySets"]:
			name, no, images = pathify(series["description"]) or "Unnamed", series["seriesNumber"], series["images"]
			dir_ = SeriesDirectory(save_to, no, name, len(images))

			tasks = tqdm(images, desc=name, unit="张", file=sys.stdout)
			for i, info in enumerate(tasks):
				# 图片响应头包含的标签不够，必须每个都请求 GetImageDicomTags。
				tags = await self.get_tags(info)

				# 没有标签的视为非 DCM 文件，跳过。
				if len(tags) == 0:
					continue

				pixels, _ = await self.get_image(info, is_raw)
				_write_dicom(tags, pixels, dir_.get(i, "dcm"))

		return save_to

	@staticmethod
	async def from_url(client: ClientSession, viewer_url: str):
		"""
		已经拿到最终查看器页的链接就使用该函数，需要先处理登录并拿到 Cookies。

		:param client: 会话对象，要先拿到 ZFP_SessionId 和 ZFPXAUTH
		:param viewer_url: 页面 URL，路径中有 /ImageViewer/StudyView
		"""
		async with client.get(viewer_url) as response:
			html4 = await response.text()
			matches = _VAR_RE.findall(html4)
			top_study_id = matches[0][1]
			accession_number = matches[1][1]
			exam_uid = matches[2][1]
			cache_key = matches[3][1]

			# 查看器可能被整合进了其它系统里，路径有前缀。
			origin, path = response.real_url.origin(), response.real_url.path
			offset = path.index("/ImageViewer/StudyView")
			client._base_url = origin.with_path(path[:offset + 1])

		# 获取检查的基本信息，顺便也判断下访问是否成功。
		params = {
			"studyId": top_study_id,
			"accessionNumber": accession_number,
			"examuid": exam_uid,
			"minThickness": "5"
		}
		async with client.get("ImageViewer/GetImageSet", params=params) as response:
			image_set = await response.json()

		return HinacomDownloader(client, cache_key, image_set)

	@staticmethod
	async def from_viewer_link(client: ClientSession, redirect_url: str):
		"""
		能进入报告页之后就使用此函数，模拟点击右边的“查看影像”的链接，
		之后是两次跳转，已发现多个网站是同样的流程所以提出来作为一个函数。

		:param client: aiohttp 的会话
		:param redirect_url: “查看影像” 链接的目标地址
		"""
		async with client.get(redirect_url) as response:
			html2 = await response.text()
			matches = _LINK_ENTRY.search(html2)

		# 典型 URL: http://<domain>/entry/viewimage?token=<base64>
		# 中间不知道为什么又要跳转一次，端口还变了。
		async with client.get(matches.group(1)) as response:
			html3 = await response.text("utf-8")
			client._base_url = response.real_url.origin()
			viewer_url = _TARGET_PATH.search(html3).group(1)

		return await HinacomDownloader.from_url(client, viewer_url)


def _write_dicom(tag_list: list, image: bytes, filename: Path):
	ds = Dataset()
	ds.file_meta = FileMetaDataset()

	# GetImageDicomTags 的响应不含 VR，故私有标签只能假设为 LO 类型。
	for item in tag_list:
		tag = Tag(item["tag"].split(",", 2))
		definition = DicomDictionary.get(tag)

		if tag.group == 2:
			# 0002 的标签只能放在 file_meta 里而不能在 ds 中存在。
			vr, key = definition[0], definition[4]
			value = parse_dcm_value(item["value"], vr)
			setattr(ds.file_meta, key, value)
		elif definition:
			vr, key = definition[0], definition[4]
			setattr(ds, key, parse_dcm_value(item["value"], vr))
		else:
			# 正好 PrivateCreator 出现在它的标签之前，按顺序添加即可。
			# DataElement 对 LO 类型会自动按斜杠分割多值字符串。
			ds.add_new(tag, "LO", item["value"])

	ds.file_meta.MediaStorageSOPClassUID = ds.SOPClassUID
	ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID

	# 根据文件体积和头部自动判断类型。
	px_size = (ds.BitsAllocated + 7) // 8 * ds.Rows * ds.Columns
	if image[16:23] == b"ftypjp2" and len(image) != px_size:
		ds.PixelData = encapsulate([image])
		ds.file_meta.TransferSyntaxUID = JPEG2000Lossless
	else:
		ds.PixelData = image
		ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian

	ds.save_as(filename)
	_dcm_to_jpg(ds, filename)


def _dcm_to_jpg(ds, dcm_path):
	"""把已构造的 DICOM 数据集转存为同名 JPG（应用窗宽窗位），失败不影响 DCM。"""
	try:
		import numpy as np
		from PIL import Image
	except ImportError:
		return  # 未安装 numpy/Pillow，跳过 JPG 转换
	try:
		arr = ds.pixel_array

		def _num(v):
			"""WindowCenter/Width 可能是单值或多值，统一取一个浮点。"""
			if v is None:
				return None
			try:
				return float(v)
			except (TypeError, ValueError):
				pass
			try:
				return float(v[0])
			except Exception:
				return None

		wc, ww = _num(ds.get("WindowCenter")), _num(ds.get("WindowWidth"))
		if wc is not None and ww is not None and ww > 0:
			lo, hi = wc - ww / 2, wc + ww / 2
			arr = np.clip(arr, lo, hi)
			arr = (arr - lo) / (hi - lo) * 255
		else:
			mn, mx = float(arr.min()), float(arr.max())
			if mx > mn:
				arr = (arr - mn) / (mx - mn) * 255

		arr = arr.astype(np.uint8)
		img = Image.fromarray(arr, mode="L") if arr.ndim == 2 else Image.fromarray(arr)
		jpg_path = str(dcm_path).rsplit(".", 1)[0] + ".jpg"
		img.save(jpg_path, quality=95)
	except Exception as e:
		print(f"JPG 转换失败（不影响 DCM）: {e}")
