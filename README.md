EvilTown

[English](#eviltown-en) | [中文说明](#eviltown-zh)

* * *

<a id="eviltown-en"></a>

## Tribute

This project pays tribute to John Conway's Game of Life.

The Game of Life looks at the life and death of cells on a grid. EvilTown borrows that grid idea, but puts people living their everyday lives on the tiles instead.

## What It Is Built On

I made this project after reading *Think Complexity*. It uses an ABCA structure (Agent-Based Cellular Automaton), divided into three straightforward parts:

* **The grid handles physics**: On a 240×160 map, tiles decide where people can walk and what blocks them. Five invisible fields also diffuse across the ground: smell from toilets, aroma from cafeterias, noise from streets, crowd density, and day/night lighting.
* **The relationship graph handles society**: When two residents get close to each other, they might chat, gossip, or argue based on their personality and mood. Sleeping people do not talk.
* **The language model handles narration**: The model is not in the main loop; it only writes resident diaries in the background. If no model is configured or if the request times out, built-in templates take over so the simulation keeps running without stuttering.

## How to Open It

Install dependencies and start the server:

    pip install -r requirements.txt
    python server.py

* **Game**: [http://127.0.0.1:8000](http://127.0.0.1:8000)Left-click and drag to move the view, mouse wheel to zoom in and out, double-click to fit the whole map. Click the gear icon in the top right to switch between Chinese and English.
* **Point Editor**: [http://127.0.0.1:8000/static/furniture-editor.html](http://127.0.0.1:8000/static/furniture-editor.html)This editor is used to mark spots on building images. You can mark toilets, beds, desks, and counters. Saving applies immediately to the running simulation without needing a restart.
* **Run tests**: `python -X utf8 -m pytest -q`

## How Some Mechanics Work

* **Time**: One step is one minute in simulation, 1440 steps per day. The middle of the speed slider is 1× speed, which takes about 5 real minutes to finish one simulation day.
* **Needs**: Residents have hunger, bladder, energy, and social needs. When hungry they eat, when nature calls they find a toilet, and when tired they head home to sleep.
* **Toilets are physical spots**: A building only has as many toilets as you marked in the editor. If occupied, others have to queue outside and wait.
* **Work and wages**: A job is a specific desk or counter. Residents only earn hourly wages while physically standing at their own workstation. Walking away mid-shift means no pay.
* **Money and financial stress**: Eating, buying fishing gear, and utilities all cost money. If a resident's bank balance has been dropping over the last 7 days, they feel stressed and become more willing to go to work.
* **Minors**: Residents under 18 will not visit the bar; the bar is automatically removed when picking destinations.
* **Snoring**: Sensitive residents sleeping in the same room with a snoring housemate recover energy much slower at night.

## License

Apache-2.0 license. See the [LICENSE](LICENSE) file for details.

* * *

<a id="eviltown-zh"></a>

# EvilTown（中文）

## 致敬

本项目向约翰·康威（John Conway）的生命游戏致敬。

生命游戏看格子的生和死；这个项目借用了格子的思路，但在格子里放了一个个生活的小人。

## 本项目基于什么

这个项目是我在读了《思考复杂性》（*Think Complexity*）之后做的。整体采用 ABCA 结构（基于智能体的元胞自动机），分成三块：

* **格子管物理**：在 240×160 的地图上，格子决定谁能走到哪、谁被墙挡住。地面上还有 5 种场在慢慢扩散：气味（厕所）、香味（食堂）、噪音（马路）、人流拥挤度，以及光照。
* **关系图管社交**：小镇里两个人走近了会根据各自性格和心情触发搭话，可能闲聊，也可能拌嘴。睡觉的人不说话。
* **大模型管叙事**：大模型不在主循环里，只在后台负责写写小人的日记。如果没有配置模型或者连不上，程序会直接用内置模板生成，小镇正常运行，不会卡住。

## 怎么打开

安装依赖并启动服务：

    pip install -r requirements.txt
    python server.py

* **游戏主界面**：[http://127.0.0.1:8000](http://127.0.0.1:8000)在网页里按住左键拖拽视野，鼠标滚轮缩放，双击回到全图。右上角齿轮可以切换中英文。
* **图片与家具点位编辑器**：[http://127.0.0.1:8000/static/furniture-editor.html](http://127.0.0.1:8000/static/furniture-editor.html)这个编辑器用来给建筑贴图标位置。可以在建筑图上标出马桶、床、工位和柜台。保存后游戏里立刻生效，不需要重启程序。
* **跑测试**：`python -X utf8 -m pytest -q`

## 一些机制

* **时间**：每一步是仿真里的 1 分钟，一天 1440 步。速度滑条中点是 1 倍速，现实里大约 5 分钟跑完一天。
* **需求**：小人有饥饿、内急、精力、社交四个需求。饿了找饭馆，急了找厕所，困了回家睡觉。
* **马桶是具体点位**：建筑里的马桶位置是固定的，有人在用就得在外面排队。
* **上班发工资**：工作岗位对应建筑里的桌子或柜台。小人必须肉身站在自己的工位上，才会按小时拿到工资。摸鱼跑开是不算工时的。
* **花钱与财务压力**：吃饭、买渔具、交水电都要花钱。如果最近几天存款一直在掉，小人会感到缺钱，更倾向于去上班。
* **未成年人**：不满 18 岁的人不会去酒吧，选目的地时会自动过滤掉。
* **打呼噜**：性格敏感的人如果遇到室友打呼噜，晚上的睡眠质量会变差。

## 许可

本项目采用 Apache-2.0 许可证，详见 [LICENSE](LICENSE) 文件。
