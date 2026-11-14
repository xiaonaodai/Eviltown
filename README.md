# EvilTown

[English](#eviltown-en) | [中文说明](#eviltown-zh)

* * *

<a id="eviltown-en"></a>

## Tribute

This project pays tribute to John Conway's Game of Life.

The Game of Life looks at the life and death of cells on a grid. EvilTown borrows that grid idea, but puts people living their everyday lives on the tiles instead.

## What It Is Built On

I made this project after reading *Think Complexity*. It is a life observation simulation based on cellular automata and autonomous agents (ABCA).

The lives of the residents evolve step by step over time. The grid is the ground where residents live and walk around. They socialize, work, sleep, eat, and use the restroom. If they get too stressed, they can fall ill; if all the toilets are occupied and someone can't hold it, things get awkward fast.

Unlike the traditional Game of Life, residents don't just mechanically follow rules to eat when hungry and sleep when tired—they possess memory and soul shaped by a large language model. Every encounter, struggle, and daily experience leaves a lasting imprint on their memory, giving them independent thought within a rule-bound world. As life unfolds, each resident's inner self and life path are genuinely molded into something unique.

## How to Open It

Install dependencies and start the server:

    pip install -r requirements.txt
    python server.py

* **Game**: [http://127.0.0.1:8000](http://127.0.0.1:8000)Left-click and drag to move the view, mouse wheel to zoom, double-click to fit the whole map. Click the gear icon in the top right to switch between Chinese and English.
* **Point Editor**: [http://127.0.0.1:8000/static/furniture-editor.html](http://127.0.0.1:8000/static/furniture-editor.html)Use this editor to mark spots on building images. You can mark toilets, beds, desks, and counters. Saving applies immediately to the running simulation without needing a restart.
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

这个项目是我在读了《思考复杂性》（*Think Complexity*）之后做的。

它是一个基于元胞自动机与多智能体（ABCA）的生活观测游戏。小人的生活会随着每一时间步慢慢演化推进。格子就是地面，是小人们生活和走动的地方。小人可以社交、工作、睡觉、吃饭、上厕所；压力大了也会生病，要是公厕全被占满了、内急憋不住了也会很尴尬。

与传统的生命游戏不同的是，每个小人除了按照规则的设定饿了吃、困了睡，还拥有由大语言模型带来的**记忆塑造与灵魂塑造**。小人们经历的每一次相遇、摩擦与柴米油盐，都会沉淀为他们独有的记忆，让小人在规则遍布的社会里拥有独立的思考。每个小人的生活际遇不同，被生活所塑造出的性格、心境与人生走向，也完全不同。

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
