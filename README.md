# 商品图 × 商家模板图片合成

一个面向外卖海报的 Python 图片合成工具。它会把商品图和商家模板合成为一张竖版海报，并尽量保持模板中的品牌信息、活动文案和底部规则说明。

项目提供两种合成策略：

- `overlay`：模板是白底/浅色平面设计时，将白色底去除，把文案和 Logo 叠到商品图上。适合你给出的美团活动模板，效果接近附件 4。
- `cutout`：模板是有真实场景的背景时，使用 `rembg` 自动抠出商品，再按模板光线添加接触阴影和环境阴影。
- `auto`（默认）：根据模板的白色占比自动选择以上策略。

## 安装

建议使用 Python 3.10+：

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
```

`rembg` 只在 `cutout` 模式使用；按上面的 `requirements.txt` 安装即可。它默认使用轻量的 `u2netp` 模型（首次运行约下载 4.6 MB），如果不安装它，程序会自动使用 OpenCV GrabCut 作为兜底。

## 命令行用法

合成一张海报：

```bash
python -m food_compositor \
  --products ./inputs/burger.png ./inputs/noodles.png \
  --template ./inputs/template.jpg \
  --output-dir ./outputs \
  --mode auto
```

常用参数：

```text
--mode auto|overlay|cutout       合成策略，默认 auto
--size 1080x1920                 输出尺寸，默认沿用模板尺寸
--logo-pill / --no-logo-pill     overlay 模式下保留左上角 Logo 白色胶囊
--outline 0..1                   为模板图形添加白色可读性描边，默认 0.78
--quality 1..100                 JPEG 质量，默认 95
```

也可以在 Python 中调用：

```python
from food_compositor import compose

compose(
    product_path="inputs/burger.png",
    template_path="inputs/template.jpg",
    output_path="outputs/burger.jpg",
    mode="auto",
)
```

## 设计要点

1. 商品图先按画布比例 `cover` 裁剪，避免拉伸食物形状。
2. 模板白底通过软阈值转透明，保留抗锯齿边缘；文案额外生成轻微描边，防止叠在复杂食物纹理上时失读。
3. 抠图模式会按照商品主体的 alpha 边界等比缩放，并生成模糊接触阴影和环境阴影，减少“贴纸感”。
4. 原图不会被覆盖，输出目录只写入新的 JPG/PNG 文件。

## 运行示例

把自己的商品图和模板图放在 `inputs/` 后运行：

```bash
python -m food_compositor \
  --products \
  ./inputs/burger.png \
  ./inputs/noodles.png \
  --template ./inputs/template.jpg \
  --output-dir ./outputs --mode auto
```

## React 前端工作台

项目现在包含一个 React + Vite 前端和 FastAPI 接口。启动后可以拖入商品图、模板图，选择合成方式，查看成品并下载 JPG。

### 一键启动生产预览

```bash
pip install -r requirements-web.txt
npm --prefix web install
npm --prefix web run build
python -m uvicorn server:app --host 127.0.0.1 --port 8000
```

打开 <http://127.0.0.1:8000>。

### 前端开发模式

终端一：

```bash
python -m uvicorn server:app --host 127.0.0.1 --port 8000
```

终端二：

```bash
npm --prefix web install
npm --prefix web run dev
```

开发地址是 <http://localhost:5173>，Vite 会把 `/api` 请求代理到 Python 服务。

前端入口在 [web/src/App.tsx](web/src/App.tsx:1)，视觉样式在 [web/src/styles.css](web/src/styles.css:1)，接口在 [server.py](server.py:1)。如需在本机显示“试用示例”，可将自己的模板图和商品图分别放在仓库根目录并命名为 `1.jpg` 和 `商品1.png`；这些本地素材不会提交到 Git。

商品图区域支持选择整个文件夹或一次多选图片；模板图保持单张。批量任务会逐张合成，前端逐张预览结果，并支持选择部分图片下载或下载全部 `food-composites.zip`。尺寸选项包含 `1280×720 · 横版`、`1080×1920 · 竖版`，以及两种移动端竖版尺寸。

横版 `1280×720` 会将商品图按比例铺满画布，并在模板已经是同尺寸时直接使用模板原始像素合成，减少 Logo 和活动文字的重复缩放。模板去白底时只移除与画布边缘连通的白色区域，会保留描边文字内部的白色填充；输出 JPEG 使用高质量、4:4:4 色度采样以保持小字边缘清晰。
