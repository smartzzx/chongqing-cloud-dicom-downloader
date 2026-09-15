"""
DICOM 下载器 GUI（仅支持 mdmis.cq12320.cn 站点）。

运行：python gui.py
"""
import asyncio
import contextlib
import io
import os
import re
import shutil
import sys
import threading
import time
import tkinter as tk
import warnings
from pathlib import Path
from tkinter import ttk, scrolledtext, filedialog

from crawlers import cq12320

# 抑制 pydicom 对私有标签值超长的 LO 类型警告（不影响文件读写）
warnings.filterwarnings("ignore", message="The value length .* exceeds the maximum length .* allowed for VR LO.")


class StdoutRedirector:
	"""把 stdout 重定向到 tkinter Text 组件。"""

	def __init__(self, text_widget):
		self.widget = text_widget

	def write(self, content):
		if not content:
			return
		self.widget.after(0, self._append, content)

	def _append(self, content):
		self.widget.configure(state="normal")
		self.widget.insert("end", content)
		self.widget.see("end")
		self.widget.configure(state="disabled")

	def flush(self):
		pass


@contextlib.contextmanager
def _suppress_native_stderr():
	"""在文件描述符层临时屏蔽 stderr。

	tkinter 的 vista 主题内嵌 PNG 带有错误的 iCCP 配置，libpng 会直接向
	C 层 stderr 打印告警，Python 的 warnings / sys.stderr 重定向均无法拦截，
	因此只在窗口创建与界面构建期间做定点屏蔽。
	"""
	try:
		saved_fd = os.dup(2)
	except OSError:
		yield
		return
	try:
		with open(os.devnull, "w") as devnull:
			os.dup2(devnull.fileno(), 2)
			yield
	finally:
		os.dup2(saved_fd, 2)
		os.close(saved_fd)


class DownloaderApp:
	def __init__(self, root):
		self.root = root
		self.root.title("重庆卫健影像云下载器")
		self.root.geometry("780x560")
		self.root.minsize(640, 420)
		self._threads = []
		self._pending = 0
		self._results = {}
		self._lock = threading.Lock()
		self._dicom_dir = None  # DICOM 保存目录（Path）
		self._pdf_tmp = None    # PDF 临时文件路径
		self._pdf_name = None   # PDF 目标文件名
		self._base_dir = str(Path.home() / "Desktop")  # 保存根目录

		self._build_ui()
		self._redirect_stdout()

	def _build_ui(self):
		# 顶部输入区
		top = ttk.Frame(self.root, padding=12)
		top.pack(fill="x", side="top")

		# 保存路径（默认桌面）
		ttk.Label(top, text="保存路径：").pack(anchor="w")
		path_row = ttk.Frame(top)
		path_row.pack(fill="x", pady=(4, 0))
		self.path_var = tk.StringVar(value=str(Path.home() / "Desktop"))
		path_entry = ttk.Entry(path_row, textvariable=self.path_var)
		path_entry.pack(side="left", fill="x", expand=True)
		ttk.Button(path_row, text="浏览…", command=self._browse_dir).pack(side="left", padx=(6, 0))

		# 分享链接
		ttk.Label(top, text="影像云分享链接：").pack(anchor="w", pady=(8, 0))
		self.url_var = tk.StringVar()
		entry = ttk.Entry(top, textvariable=self.url_var)
		entry.pack(fill="x", pady=(4, 0))
		entry.bind("<Control-a>", self._select_all)

		# 选项
		opts = ttk.Frame(top)
		opts.pack(fill="x", pady=(8, 0))
		self.raw_var = tk.BooleanVar(value=False)
		tk.Checkbutton(opts, text="下载未压缩像素（--raw，默认 JPEG2000 无损）",
					   variable=self.raw_var).pack(anchor="w")

		# 中部日志区（自适应填充剩余空间）
		mid = ttk.Frame(self.root, padding=(12, 0))
		mid.pack(fill="both", expand=True, side="top")
		ttk.Label(mid, text="下载日志：").pack(anchor="w")
		self.log = scrolledtext.ScrolledText(
			mid, height=12, state="disabled", wrap="word"
		)
		self.log.pack(fill="both", expand=True, pady=(4, 0))

		# 底部按钮区（固定底部，始终可见；单按钮同时启动 DICOM 与 PDF）
		bottom = ttk.Frame(self.root, padding=12)
		bottom.pack(fill="x", side="bottom")
		self.btn = tk.Button(
			bottom, text="开始下载",
			height=1, bg="#2563eb", fg="white", activebackground="#1d4ed8",
			activeforeground="white", relief="flat", cursor="hand2",
			command=self._on_download,
		)
		self.btn.pack(fill="x", ipady=6)

	def _select_all(self, _event):
		_ = self.root.focus_get()
		if isinstance(_, tk.Entry):
			_.select_range(0, "end")
			_.icursor("end")
		return "break"

	def _redirect_stdout(self):
		sys.stdout = StdoutRedirector(self.log)

	def _browse_dir(self):
		d = filedialog.askdirectory(initialdir=self.path_var.get())
		if d:
			self.path_var.set(d)

	def _log(self, msg):
		self.log.configure(state="normal")
		self.log.insert("end", msg)
		self.log.see("end")
		self.log.configure(state="disabled")

	def _on_download(self):
		url = self.url_var.get().strip()
		if not url:
			self._log("请先粘贴分享链接。\n\n")
			return
		if "mdmis.cq12320.cn" not in url:
			self._log("仅支持 mdmis.cq12320.cn 站点链接。\n\n")
			return
		if self._threads and any(t.is_alive() for t in self._threads):
			return

		# 清空旧日志
		self.log.configure(state="normal")
		self.log.delete("1.0", "end")
		self.log.configure(state="disabled")

		base_dir = self.path_var.get().strip() or str(Path.home() / "Desktop")
		self.btn.configure(state="disabled", text="下载中…")
		self._threads = []
		self._results = {}
		self._pending = 2
		self._dicom_dir = None
		self._pdf_tmp = None
		self._pdf_name = None
		self._base_dir = base_dir

		# 任务1：DICOM 影像
		args = [url]
		if self.raw_var.get():
			args.append("--raw")
		t1 = threading.Thread(target=self._run_dicom, args=(args, base_dir), daemon=True)
		# 任务2：电子报告 PDF
		t2 = threading.Thread(target=self._run_pdf, args=(url, base_dir), daemon=True)
		self._threads = [t1, t2]
		t1.start()
		t2.start()

	def _run_dicom(self, args, base_dir):
		try:
			print("\n========== 医学影像云下载-仅供个人学习研究使用==========")
			result = asyncio.run(cq12320.run(*args, base_dir=base_dir))
			with self._lock:
				self._dicom_dir = result  # Path 或 None
				self._results["dicom"] = "✓ DICOM 下载完成"
		except Exception as e:
			with self._lock:
				self._results["dicom"] = f"✗ DICOM 下载失败：{e}"
		finally:
			self.root.after(0, self._on_one_done)

	def _run_pdf(self, url, base_dir):
		try:
			print("\n========== 电子报告（PDF）导出 ==========")
			from playwright.sync_api import sync_playwright
			tmp, name = self._export_report_pdf(url, sync_playwright, base_dir)
			with self._lock:
				self._pdf_tmp = tmp
				self._pdf_name = name
				self._results["pdf"] = "✓ 报告已生成（待移入影像目录）"
		except Exception as e:
			with self._lock:
				self._results["pdf"] = f"✗ 报告导出失败：{e}"
		finally:
			self.root.after(0, self._on_one_done)

	def _on_one_done(self):
		self._pending -= 1
		if self._pending > 0:
			return  # 等另一个任务完成
		# 两个任务都结束：把 PDF 从临时文件移入影像目录（与 DICOM 同级）
		with self._lock:
			pdf_tmp = self._pdf_tmp
			pdf_name = self._pdf_name
			dicom_dir = self._dicom_dir
		if pdf_tmp and pdf_name and os.path.exists(pdf_tmp):
			# DICOM 成功则放其目录下；失败则放所选根目录
			target_dir = Path(dicom_dir) if dicom_dir else Path(self._base_dir)
			try:
				target_dir.mkdir(parents=True, exist_ok=True)
				final = target_dir / pdf_name
				shutil.move(pdf_tmp, str(final))
				with self._lock:
					self._results["pdf"] = f"✓ 报告导出完成：{final}"
			except Exception as e:
				with self._lock:
					self._results["pdf"] = f"✗ 报告移动失败：{e}"
		# 统一汇总
		lines = ["", "========== 下载汇总 =========="]
		for k in ("dicom", "pdf"):
			if k in self._results:
				lines.append(self._results[k])
		lines.append("")
		self._log("\n".join(lines))
		self.btn.configure(state="normal", text="开始下载")

	def _export_report_pdf(self, url, sync_playwright, base_dir):
		with sync_playwright() as p:
			try:
				browser = p.chromium.launch(channel="msedge", headless=True)
			except Exception:
				browser = p.chromium.launch(headless=True)
			page = browser.new_page(viewport={"width": 1920, "height": 1080})
			print("加载报告页面...")
			page.goto(url, wait_until="networkidle", timeout=60000)
			page.wait_for_timeout(3000)
			print("页面标题:", page.title())

			# Vant 表单：值在 .van-cell__value 下的 input.value
			fields = page.evaluate("""() => {
				const find = (label) => {
					const labels = Array.from(document.querySelectorAll('label'));
					for (const el of labels) {
						if (el.textContent.trim() === label) {
							const cell = el.closest('.van-cell');
							if (cell) {
								const val = cell.querySelector('.van-cell__value');
								if (val) {
									const input = val.querySelector('input, textarea');
									if (input) return input.value;
									const t = val.textContent.trim();
									if (t) return t;
								}
							}
						}
					}
					return '';
				};
				return {
					accession_number: find('检查号'),
					report_date: find('报告日期'),
					exam_type: find('检查类型'),
					patient_name: find('患者姓名')
				};
			}""")
			print("提取字段:", fields)

			accession = fields.get("accession_number", "").strip() or "未知检查号"
			report_date = fields.get("report_date", "").strip()
			exam_type = fields.get("exam_type", "").strip() or "未知类型"
			patient = fields.get("patient_name", "").strip() or "未知患者"
			# 报告日期取日期部分（去掉时间和冒号）
			date_part = report_date.split(" ")[0] if report_date else "未知日期"

			# 文件名：患者姓名_检查号_报告日期_检查类型_检查报告.pdf（清理非法字符）
			name = f"{patient}_{accession}_{date_part}_{exam_type}_检查报告.pdf"
			safe_name = re.sub(r'[\\/:*?"<>|]', "_", name)
			os.makedirs(base_dir, exist_ok=True)
			# 先写到临时文件，待 DICOM 目录确定后再移入（与影像同级目录）
			tmp_path = os.path.join(base_dir, f".tmp_report_{int(time.time())}.pdf")

			# 测量内容实际尺寸，按比例导出（宽度 297mm）
			dims = page.evaluate("""() => {
				let maxR = 0, maxB = 0;
				document.querySelectorAll('*').forEach(el => {
					const r = el.getBoundingClientRect();
					if (r.right > maxR) maxR = r.right;
					if (r.bottom > maxB) maxB = r.bottom;
				});
				return {w: Math.ceil(maxR), h: Math.ceil(maxB)};
			}""")
			print("内容尺寸(px):", dims)

			target_w_mm = 297
			target_h_mm = round(target_w_mm * dims["h"] / dims["w"], 1)
			page.pdf(path=tmp_path, width=f"{target_w_mm}mm", height=f"{target_h_mm}mm",
					 print_background=True,
					 margin={"top": "0", "bottom": "0", "left": "0", "right": "0"})
			browser.close()
			print(f"报告已生成（临时）: {tmp_path}（{target_w_mm}×{target_h_mm}mm）")
			return tmp_path, safe_name


def main():
	# 窗口创建、主题加载、界面构建及首次绘制期间屏蔽 libpng iCCP 告警
	with _suppress_native_stderr():
		root = tk.Tk()
		root.style = ttk.Style()
		try:
			root.style.theme_use("vista")
		except tk.TclError:
			pass
		DownloaderApp(root)
		root.update_idletasks()
	root.mainloop()


if __name__ == "__main__":
	main()
