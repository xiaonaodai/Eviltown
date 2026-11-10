# EvilTown

A small town that lives on its own. Fifty people on a grid, nobody telling them what to do.

[中文说明在下面 ↓](#eviltown-中文)

---

## English

### What this is

A grid of 240 × 160 tiles. Homes, offices, a school, a canteen, shops, a park, a bar, a fishing pond.

Fifty residents live here. Everyone has a fixed home, a bed, a job, a personality, and a small circle of friends.

The sim moves **one minute per step**. 1440 steps is a day. It starts at 06:00.

Nobody takes orders from you. They check the clock, weigh what they need, and pick where to go next. Hungry — find food. Tired — go home. Bored — find someone to talk to.

> In one line: **the grid handles physics, the graph handles society, the LLM handles storytelling.** The LLM part is still parked — everything you see today comes from rules and templates.

### Run it

```bash
pip install -r requirements.txt   # first time only
python server.py
```

Open <http://127.0.0.1:8000>. That's it.

Close the tab whenever you like. The server keeps running. Come back later and the town is still there, still going.

### Connecting a model (optional)

The town runs fine with no model — wishes, diaries and chats fall back to templates.
Connect one when you want real prose.

```bash
cp .env.example .env        # Windows: copy .env.example .env
```

Then fill three lines in `.env`:

```ini
EVILTOWN_LLM_BASE_URL=                           # any OpenAI-compatible endpoint, ends in /v1
EVILTOWN_LLM_MODEL=                              # the model it serves
EVILTOWN_LLM_KEY=                                # blank for local servers
```

Restart the server — `.env` is read once at startup, so editing it while the town runs changes nothing.
Local servers (llama.cpp `:8080`, LM Studio `:1234`, Ollama `:11434`) don't check the key; hosted ones
(DeepSeek, OpenAI, Moonshot, Qwen) require one. `.env.example` has the full notes.

From then on each day's diary is rewritten by the model right after the day ends, and the markdown under
`data/reports/diary/` says `source: llm`. If the model is slow, dead or talks nonsense, that day just
stays on the template — the town never waits for a model.

**Never commit `.env`.** It's already in `.gitignore`, and `config.yaml` only holds `${EVILTOWN_LLM_*}`
references, so no key ever lands in the repo.

### Changing the language

Click the **⚙** button in the top right corner. There's a **Language** row in the panel.

- Pick `English` or `中文`. It switches **right away** — no page refresh.
- Your choice is remembered the next time you open the page.
- A fresh browser starts in **English**.

### Things worth touching

- **Speed slider** (top bar). The middle is **1×** — a full day takes about **4.8 minutes**. Pull left to slow down (0.1× at the far left, one day in 48 minutes — good for following a single person). Pull right to speed up (10×, one day in half a minute). The slider is logarithmic, so one notch near the middle only changes things by ~10%.
- **Names** checkbox — draw or hide resident names on the map.
- **Zoom** — `−` / `＋` / `适应`. The mouse wheel zooms at the cursor, hold and drag the left button to pan, double-click to fit the whole map.
- **Click a person** → the side panel shows their needs, mood, money, job, personality, friends, wish, and what they've been doing.
- **Click a building** → change its opening and closing hours. Takes effect immediately.
- Three side tabs: **data / log / daily report**. The report breaks a day into eight 2-hour buckets plus one whole-night bucket.
- **场热力 dropdown** — overlay the CA fields (smell, noise, crowd, light, aroma) as a heat map.

### Placing furniture points (the texture editor)

There is no button for this in the main page. Open it directly:

<http://127.0.0.1:8000/static/furniture-editor.html>

**Why you'd care.** Nothing indoors is placed randomly any more. Toilets, beds, desks and counters all come from `points.json`. No point, no toilet, no bed. And a `desk` or `counter` point *is* a job — drop one more point, and the town has one more post.

**How it works:**

1. Pick a building kind on the left (home, office, canteen…).
2. Pick one of its images. Points are stored **per image**, so two houses with different textures can have completely different layouts.
3. Pick a point type — keys `1`–`8`: toilet, bed, table, desk, seat, counter, door, idle spot.
4. Click on the image. Drag a point to move it, right-click to delete, arrow keys to nudge (`Shift` for bigger steps).

Then hit **Apply**. It writes `points.json` (path from `furniture.points_path` in `config.yaml`; a `.bak` is made first) and **hot-applies to the running sim** — no restart, no residents lost.

The editor is bilingual. It shares the site's language setting (`eviltown.lang` in `localStorage`): whatever you picked in the main page's ⚙ menu applies here too, and there's also a `中文 / English` dropdown in the editor header to switch on the spot. First visit defaults to English, same as the main page.
Switch the language in one page and the other one follows immediately — no reload needed.

The other buttons:

- **Export / Import** (导出 / 导入) — save the points as a file, or load one back.
- **Clear** (清空本类) — clears only the image you're looking at, not the whole kind.
- **Auto cutout / Paint & cut** (自动去背景 / 涂抹背景 + 按涂抹抠图) — cut the background out of a texture. Auto mode guesses from the four edges of the image; when that fails (full-bleed illustration, checkerboard backdrop) paint over the background and cut by your strokes. Untick "grow" and it removes **only** what you painted — no spreading. The generated PNG never overwrites the original JPG, and the points follow the image.

Coordinates are stored as relative fractions (0–1), so one set of points fits any building size.

### What's actually running

**Needs and mood.** Hunger, bladder, fatigue and social needs decay every minute. Personality feeds into it: low agreeableness means you don't go looking for people, high neuroticism means stress piles up faster and you deal with it differently.

**Economy.** A job is a `desk` or `counter` point placed in the furniture editor — add a point, add a job. Adults are born unemployed; students and children don't look for work. One person per post. Wages are 26–35 per hour and only get paid while you're standing on your own post. Meals cost 12 at the canteen or 60 at a restaurant. A bar night is 120. Fishing gear is 600, one-time and reusable. Doctor visits are free. Water and power cost 6 a day. Selling a fish pays 25. You can go negative — debt is allowed.

**Financial stress follows the slope, not the balance.** Each person keeps seven days of balance history. If your wallet is shrinking 12.5% a week, you're at full panic even if you still have ten thousand. If it's growing, you stay calm.

**Relationships.** People notice each other from 3 tiles away, less likely to speak the further apart they are. Sleeping people don't talk. One conversation per person per minute. Strangers become acquaintances after a few of the same kind of interaction, and it climbs from there.

**Smell, noise, aroma, crowds, light, snoring.** All of them feed into mood or behaviour now:
smell and noise pull mood down, aroma lifts it, crowds attract extroverts and repel introverts, darkness nudges people home for the night without ever locking them in, and a roommate who snores cuts your sleep recovery in half — but only if you're the high-neuroticism type who can't sleep through it.

**Daily report and SQLite.** Every day gets written to disk: a snapshot of residents and relationships, an event stream, and a stats table behind `/api/report`.

### Tests

```bash
python -X utf8 -m pytest -q
```

17 tests, about a minute.

#### Names in two languages

Every resident has a Chinese name (`施磊`) and an English one (`Henry`). The English name is
**first-name only** — no surname, no transliteration pairing — and it is what the whole UI shows
when the language is English: the label over each head on the map, the log list, the event feed,
the resident panel, the hover bubble, and the diary markdown. Chinese stays the single source of
truth for saves and the database, so switching languages never renames the same person twice.

## Not built yet

- **LLM slow brain.** Clients, wishes and diary export exist, but wishes are display-only and every diary is currently the template version. No model is connected.
- **Playback.** The data is stored, the scrubber isn't.
- **Police and crime.** Stealing is one of the 24 interactions. There are no consequences.
- **Birth and death.** The population stays at fifty.
- **Weather and seasons.**
- **A relationship graph view.** The data is there; it's only shown as a list inside one person's panel.

Every number in this project, and every mistake made getting there, is written up in [DESIGN.md](DESIGN.md).

---

## EvilTown 中文

### 这是个什么

一张 240 × 160 的格子。上面有住宅、办公楼、学校、食堂、商店、公园，还有一个酒吧和一个钓鱼池塘。

五十个人住在这里。每人有自己的家、自己的床、一份工作、一套性格、几个熟人。

程序**一分钟走一步**，1440 步是一天，早上六点开局。

没人听你指挥。他们自己看钟点、掂量需求、决定下一分钟去哪。饿了找吃的，困了回家睡，闷了找人说话。

> 一句话：**格子管物理，关系管社会，大模型管讲故事。** 大模型那一层还停在纸面上——现在你看到的全来自规则和模板。

### 怎么跑起来

```bash
pip install -r requirements.txt   # 第一次才要
python server.py
```

打开 <http://127.0.0.1:8000>，小镇就开始走了。

网页关了没关系，服务还在跑，小镇也还在走。过一会儿再打开，它还在那儿。

### 接上大模型（可选）

不接模型也能一直跑 —— 愿望、日记、聊天都退回内置模板。想要像样的文字，再接。

```bash
cp .env.example .env        # Windows：copy .env.example .env
```

然后改 `.env` 里这三行：

```ini
EVILTOWN_LLM_BASE_URL=                           # 任何 OpenAI 兼容地址，以 /v1 结尾
EVILTOWN_LLM_MODEL=                              # 服务里加载的模型名
EVILTOWN_LLM_KEY=                                # 本地起的留空
```

改完重启服务就生效 —— `.env` 只在启动时读一次，跑着的时候改它没用。本地服务（llama.cpp
`:8080`、LM Studio `:1234`、Ollama `:11434`）不校验 key，云端（DeepSeek、OpenAI、Kimi、通义）
必须填。换服务就改前两行，`.env.example` 开头有完整说明。

接上之后，每天结束后模型会在后台把日记重写一遍，`data/reports/diary/` 里的 md 会从
`source: template` 变成 `source: llm`。模型挂了、慢了、胡说八道都不影响小镇 —— 那一天就停在
模板版，仿真从不等待模型。

**`.env` 千万别提交。** `.gitignore` 里已经排除了；`config.yaml` 里只写 `${EVILTOWN_LLM_*}`
引用，仓库里不会出现任何密钥。

### 怎么换语言

**右上角那个 ⚙ 齿轮**，点开，面板里有一行「语言」。

- 选英文或中文，**点完立刻变，不用刷新**。
- 下次打开还记着你选的。
- 新浏览器第一次进来，**默认是英文**。

### 几个值得玩的地方

- **速度滑条**（顶栏）。**中点就是 1×**，一天约 **4.8 分钟**走完。往左拨是慢放——最左 0.1×，一天走 48 分钟，适合盯住一个人看；往右拨是加速——最右 10×，一天半分钟。滑条是对数刻度，中点附近拨一格只变一成左右，不会一下翻倍。
- **名字**勾选框：地图上要不要显示人名。
- **缩放**：`−`、`＋`、`适应`。滚轮以鼠标为锚点放大，按住左键拖动平移，双击回到整图。
- **点一个人**：右侧面板出他的需求、心情、钱包、职业、性格、熟人、愿望，以及最近在干什么。
- **点一栋楼**：能改开门和关门时间，改完马上生效。
- 三个页签：**数据 / 日志 / 日报**。日报把一天切成八个两小时的段，再加一整段「夜里」。
- **场热力下拉框**：能叠气味、噪声、人流、光照、香味五张热力图看。

### 摆点位（贴图编辑器）

这个页面主站里**没有入口按钮**，直接打开：

<http://127.0.0.1:8000/static/furniture-editor.html>

**为什么要用它。** 室内现在不随机生成了。马桶、床、工位、柜台全部来自 `points.json` —— 没点位就真的没有马桶、没有床。而且一个 `desk` 工位或 `counter` 柜台点位**本身就是一份工作**：多点一个点位，全镇就多一个岗位。

**怎么用：**

1. 左边选业态（住宅、办公楼、食堂……）。
2. 选一张贴图。点位按**每张图**存，所以两家贴图不同的房子可以有完全不同的布局。
3. 选点位类型（数字键 `1`–`8`）：马桶、床、桌子、工位、座位、柜台、入口、闲逛热点。
4. 在图上点。拖动点位可以移动，右键删除，方向键微调（按住 `Shift` 粗调）。

改完点 **应用** —— 写进 `points.json`（路径见 `config.yaml` 的 `furniture.points_path`，写之前自动备份 `.bak`），并**立刻热更新到正在跑的仿真**：不用重启，居民一个都不丢。

这一页**中英双语**，跟主站共用同一份语言设置（`localStorage` 里的 `eviltown.lang`）—— 你在主站 ⚙ 里选了中文，这里就是中文；页头也有个 `中文 / English` 小下拉，可以当场切。第一次打开默认英文，跟主站保持一致。
在主站切了语言，已经打开的编辑器页面会立刻跟着变，不用刷新。

另外几个按钮：

- **导出 / 导入**：把点位存成 json 文件，或从文件读回来。
- **清空本类**：只清当前这一张图的点位，同类的其他贴图不受影响。
- **自动去背景 / 涂抹背景 + 按涂抹抠图**：给贴图抠背景。自动模式从图片四边推测背景；碰上满幅插画、棋盘格这种猜不出来的，就涂一下背景再按涂抹抠 —— 把「按颜色向外生长」的勾去掉，就**只删你涂过的地方，绝不蔓延**。抠出的 PNG 不会覆盖原图，点位会跟着搬到新图名下。

点位存的是相对比例（0~1），所以同一套点位能套在任何尺寸的建筑上。

**地图上的点位是什么颜色。** 主站右侧有一块「点位颜色」图例：马桶白、床浅蓝、桌子橙、工位紫、座位绿、柜台粉、门口浅灰、闲逛热点灰蓝 —— 8 种点位各一个色块，和编辑器里点位用的是**同一张色表**。所以在编辑器里摆在床或工位上的小点，回到游戏地图上认得出是同一个东西；切英文时图例名字跟着变。

### 现在到底在跑什么

**需求和心情。** 饿、内急、累、想说话，每分钟都在涨。性格会掺进来：亲和低的人不爱凑人；神经质高的人压力积得快，发泄的方式也不一样。

**钱。** 岗位就是你在点位编辑器里放的每一个 `desk` 工位或 `counter` 柜台——加一个点位就多一个岗位。成年人出生时都待业，学生和小孩不找工作。一个位子只坐一个人。工资每小时 26 到 35 块，**只有站在自己工位上才进账**。食堂一顿 12，下馆子 60，酒吧一晚 120，买渔具 600（一次买断、能反复用），看病不要钱（医保），水电每天 6 块，卖一条鱼挣 25。允许欠钱——账上可以是负数。

**财务焦虑看的是「变穷的速度」，不是「穷到什么程度」。** 每人存着七天的余额记录，一周缩水 12.5% 就算慌到顶——哪怕账上还有一万。反过来一周在涨钱，存款不算厚也不慌。

**关系。** 三格之内能看见彼此，隔得越远越不容易开口。睡觉的人不搭话，一个人一分钟最多跟一个人说话。几个来回之后陌生人变认识，再往上还有朋友、挚友、恋人。

**气味、噪声、香味、人流、光照、打呼。** 这些现在都真的影响到人：
臭味和噪声往下拽心情，香味往回补；热闹的地方外向的人往里凑、内向的人往外躲；天黑只是让人更想回家，不是把人锁屋里；室友打呼会让你睡不回来——但只有神经质高、本来睡得浅的人才会中招。

**日报和落库。** 每天都会写进库里：一份居民和关系快照、一串事件、一张统计表（`/api/report` 就是读它）。

### 测试

```bash
python -X utf8 -m pytest -q
```

十七个用例，一分钟左右。

#### 名字有两种语言

每个居民都有中英两个名字：中文名 `施磊`、英文名 `Henry`。英文名**只有名、不带姓**（不是
`Henry Shi`），也不跟中文姓配对 —— 就是独立抽的。切到英文后，全站都用它：地图上小人头顶的
标签、日志列表、事件流、居民详情面板、鼠标悬停气泡、导出的日记 md。中文名仍是存档与落库的
唯一真相，所以来回切语言不会把同一个人变成两个人。

## 还没做的

- **大模型慢脑。** 客户端、愿望、日记导出都写好了，但愿望目前只显示不驱动行为，每天的日记也全是模板版——**没有任何模型接上**。
- **回放。** 数据都存了，时间轴还没做。
- **警察和犯罪后果。** 偷窃是 24 种交互之一，但偷完没有任何事发生。
- **生死。** 一直是五十个人，不会出生也不会死。
- **天气和季节。**
- **关系图。** 数据是全的，但现在只能在单个居民的详情里看成一列文字。

每个数字是怎么定的、中间踩过哪些坑，都写在 [DESIGN.md](DESIGN.md) 里。
