# -*- coding: utf-8 -*-
"""按竞赛模板生成技术文档 DOCX。

用法：python tools/build_tech_doc.py [输出路径]
模板：需要的材料/技术文档模板.docx（保留其样式与页面设置，正文由本脚本重建）
"""
import sys
from pathlib import Path

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt, Cm

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "需要的材料" / "技术文档模板.docx"
DEFAULT_OUT = ROOT / "提交" / "技术文档.docx"

TITLE = "面向室内巡检的语音交互智能机器狗"

ABSTRACT = """本作品面向室内巡检、楼宇巡查与机器人教学演示等场景，设计并实现了一套以语音为唯一交互入口的四足机器狗系统：操作者说出"小狗，去找数字八"，机器狗自主转身搜索、走近目标卡片、停稳后读取卡面手写数字并语音作答。系统由四段链路组成——笔记本主机负责语音识别与任务编排，ZYNQ-7020 可编程逻辑承担硬件级安全闸门，四足机器人本体执行运动，感知板负责相机采图与状态中转。

本作品的工程重点不在"把现成模块接起来"，而在三个可被量化验证的问题上：其一，把"停不下来"这类致命风险交给硬件——PL 内实现 200 ms 看门狗、粘性故障闩锁与指令斜坡整形，即使上位机死机或丢包，速度命令也会在 200 ms 内被硬件归零；其二，把"走不准"变成可测的闭环——修正了状态帧航向单位被重复换算的缺陷，使转向误差从 +73° 量级收敛到 ±1.6°，并加入位置保持使整轮扫视净漂移从每次 0.3 m 降到 0.14 m；其三，把"看不清"拆成检测与识别两件事分别治理——卡片检测引入亮度、对比度、尺寸三道门槛，识别采用"停稳再拍 + 连拍挑帧 + 质量门 + 双模型级联"，把真实场景下的读数从"自信读错"变为"读不准就说看不清"。

系统已在上板环境完成验证：安全闸门自检 27 项全部通过，转向 3/3 通过（误差 −1.1°、−0.9°、+1.6°），直行 0.504 m（横向偏差 0.006 m），卡面数字识别在粗黑马克笔条件下连续 10 次全部正确，单元测试 313 项通过。"""

FUNC = """系统只有一条主交互链路，其余能力都围绕它展开。

语音交互：常驻代理监听麦克风，说出"小狗，去找数字八"即启动一次任务；"读一下数字""去三号位置""停""小狗下班"等指令走同一套规则意图解析，播报语句固定为 12 条模板，便于现场与评测。

自主找卡与读数：机器狗原地按九个停点（0°、±40°、±80°、±120°、±160°）扫视 360°，逐点检测卡片；命中目标数字后对准、走近至卡片足够大、停稳后连拍四帧、逐帧打分、只对合格帧投票，输出数字与置信度并语音播报。

安全行为：运动指令必须先经 ZYNQ 安全闸门，闸门关闭时任何速度被硬件归零；前方 0.40 m 内有障碍时拒绝前进；每次动作前重发控制权码，动作结束自动交还手动控制。

可观测性：每一步都有日志与落盘证据（帧、读数、位姿），既用于现场排障，也作为性能指标的原始数据。"""

APPLY = """室内巡检与楼宇巡查：值守人员口述目标（"去三号点位看看数字"），机器狗移动到指定区域并读取现场标识，替代重复性、低价值的走动与抄录。

机器人教学与科研演示：系统的四段链路（语音—硬件安全—运动—视觉）边界清晰、接口明确，每一段都能独立演示与考核，适合作为嵌入式、FPGA、机器人与语音交互课程的综合作业平台。

竞赛与科普展示：语音到行动的端到端时延在秒级，现场观众可直接下达口令，具备良好的演示性与传播性。"""

TECH = """1）硬件安全闸门：在 ZYNQ 可编程逻辑中以 AXI4-Lite 从设备形式实现看门狗、故障闩锁与指令斜坡整形，上位机每 20 ms 喂狗一次，超时 200 ms 自动归零并置位故障位，需显式清除才能恢复。

2）位置保持的转向闭环：以状态流中的航向与位置为反馈，一次转向在 1.8 s 内完成 45°、误差 ±1.6°；转向过程中同步做位置保持，抑制转身带来的前后位移。

3）检测与识别分离：卡片检测由亮度、对比度、绝对尺寸与边缘完整性四道门槛把关；数字识别由"连拍挑帧 + 逐帧质量门 + 主备模型级联"完成，低置信度不下断言。

4）纯 CPU 可控算力：视觉链路仅依赖 NumPy/SciPy/scikit-learn，单帧检测 34–44 ms、识别 43–47 ms，全部在笔记本 CPU 上完成，便于现场部署与复现。"""

PERF_ROWS = [
    ("安全闸门自检", "gate_test.py 全部检查项", "27/27 通过"),
    ("看门狗超时", "硬件归零实测", "200.6 ms"),
    ("指令斜坡", "每 1 ms 最大速度增量", "20 mm/s"),
    ("转向精度", "45°、45°、-90° 三次", "误差 -1.1°、-0.9°、+1.6°"),
    ("直行精度", "前进 0.50 m", "0.504 m，横向 0.006 m"),
    ("扫视净漂移", "九个停点一轮", "0.137 m"),
    ("卡面识别", "粗黑马克笔、连续 10 次", "10/10 正确"),
    ("场景基准", "200 张标准场景图", "195/200（97.5%）"),
    ("压力集", "52 张斜视角/遮挡图", "44/52（84.6%）"),
    ("墨色集", "铅笔/浅铅笔/蓝笔等", "96.2%"),
    ("语音识别", "8 段真机音频", "字错率 2.38%"),
    ("单元测试", "pytest 全量", "313 项通过"),
]

INNOV = """1）把"停机安全"下沉到 PL：速度命令必须穿过硬件闸门，超时由硬件归零，属于"软件失效也停得住"的设计。
2）面向手写卡片的"质量优先"读数策略：宁可说"看不清"，也不输出高置信度的错误数字。
3）示教—复现式定位与视觉交接：以站点位姿复现代替完整 SLAM，卡片进入视野即由视觉接管微调。"""

FLOW = """需求拆解为四条链路分别验证：先做语音到意图的离线回归，再做硬件闸门的上板自检，然后是运动闭环的精度标定，最后是视觉读数在真实场地上的收敛。每一段通过后再串联，串联阶段只修接口不修算法，保证问题定位在单一层内。"""

OMNI = """系统由四段链路组成，自上而下分别是：语音与任务编排（笔记本主机）、硬件安全闸门（ZYNQ 平台）、运动执行（四足机器人本体）、感知与读数（相机与 PC 端视觉）。四者通过千兆局域网与 UDP 报文连接，形成"指令下达—安全校验—动作执行—状态回传—视觉确认"的闭环。

主机侧：常驻语音代理负责唤醒、识别、意图解析与播报，同时维护状态订阅与运动执行器；所有对外动作都经由执行器封装，具备前置检查（站立、电量、错误码、状态新鲜度）与减速停车。

闸门侧：安全闸门以 AXI4-Lite 从设备挂在 ZYNQ 的 PS 上，上位机通过 20 ms 周期的速度报文喂狗，闸门对绝对速度做限幅与斜坡整形后输出给机器人；超时、急停或收到停止命令时，输出被立即置零并锁存故障位。

本体侧：机器人接受体坐标系线速度与偏航角速度，回传 100 Hz 状态帧（含位姿、姿态角、速度、电量、超声与错误码）。

感知侧：相机固定在机器狗头部，采图服务以 20 fps 输出 JPEG；PC 端完成卡片检测、透视归一与数字识别。"""

HW_OVERALL = """硬件分为三块：ZYNQ-7020 开发板（安全闸门与配套的 AXI 外设）、四足机器人本体（含运动主控与头部相机）、以及局域网与供电。

ZYNQ 侧不承担业务逻辑，只承担"最后一道关"：速度报文进来先做合法性判断，再做限幅与斜坡整形，最后经看门狗门控输出。这样即使上位机进程被杀、网线被拔，机器人也不会以"最后一个速度"持续跑。

机器人本体为采购平台，本作品不改动其机械结构，仅通过其开放的运动接口与状态流接入；相机为头部自带彩色模组，采图服务运行在另一块单板计算机上，作为独立感知节点。"""

MECH = """本作品不改动机器人本体的机械结构，机械工作集中在"可复现的场地布置"与"相机视角的稳定"两件事上：

（1）场地：地面铺设棕色硬纸壳，上置白纸卡片。这一布置不是美化，而是让小尺寸纸质卡片在浅色地砖上具备足够的亮度与对比度差异，使检测门槛可以稳定工作；实测无此背景时同一张卡片的检出率明显下降。

（2）相机：相机固定于头部俯视前方地面，视野中心即光轴。为兼顾"远处的卡看得见"与"近处的卡不被下边缘切掉"，系统以卡片在图像中的像素宽度作为距离代理量，取 110–140 px 为可读区间，并由运动闭环把卡片推入该区间。

（3）走线：网线与供电沿机身侧后方固定，避免遮挡相机视野与干扰超声。"""

CIRCUIT = """电路部分的核心是 ZYNQ 平台上的安全闸门，其 AXI4-Lite 从设备接口与寄存器映射如下。

（1）时钟与复位：闸门工作在 27.777 MHz（由 PS 时钟分频得到，实测比值 1.000），复位为低有效同步复位。

（2）寄存器组：0x00 版本号（读 0x00010000）、0x04 状态位（闸门开合、使能、急停、故障）、0x08 命令字（写使能/清除故障/急停）、0x10–0x18 速度命令（vx/vy/wz）、0x1C–0x24 实际输出、0x28 看门狗超时配置（默认 200 ms）、0x2C 超时计数、0x30 指令计数、0x38 斜坡步数。

（3）看门狗：每次写入速度命令即重装载计数器，计数到超时值后输出归零并置故障位；故障位为粘性，只能通过清除命令复位。

（4）斜坡整形：在 1 ms 节拍上把输出向命令逼近，每拍最大变化 20 mm/s（角速度 20 mrad/s），使急停与起步都不产生阶跃；实测 0→500 mm/s 恰好经过 20 步，与设计一致。

（5）关键约束：斜坡增量以模块参数形式下发，必须声明为整型参数，否则综合工具会把它当作字符串处理，导致限速失效——这是本作品在真实工程中踩到并修复的一个隐蔽缺陷。"""

SW_OVERALL = """主机软件分为五层：输入层（麦克风/相机/状态流）、解析层（ASR 与规则意图）、决策层（任务状态机）、执行层（运动执行器与视觉读数）、输出层（语音播报与日志）。

所有对外动作都收拢在执行器里：执行器在每次动作前检查机器人是否站立、电量是否充足、错误码是否为零、状态是否新鲜，任一条件不满足就拒绝动作并给出可播报的原因，而不是让上层逻辑各自判断。

视觉读数采用"检测—质量评估—识别—表决"四步：先检测卡片，再对帧做质量评估（卡片宽度、清晰度、对比度、是否被画面边缘切掉），只把合格帧送进分类器，最后按投票结果输出；若没有合格帧，返回"看不清"而不是硬猜。"""

SW_MODULES = """（1）语音识别与意图解析：采用离线中文识别模型，处理长窗口与断句保护；意图解析为规则匹配，覆盖"找数字 N""读一下数字""去 N 号位置""停""结束"五类，无法匹配时统一回"没听懂，再说一遍"。

（2）运动执行器：封装转向前置检查、闭环转向、直线前进、原地停车与位置保持。转向采用比例控制，死区 3°，连续三拍确认后结束；位置保持把世界坐标系下的位置误差投影到机体系，形成反向平移速度，死区 2 cm、限幅 0.14 m/s。

（3）扫视与对准：按九个停点覆盖 360°，每个停点先读后对准；只有读到目标数字而置信度不足时才再次对准并重读，避免"正对卡片还要先转一下"。

（4）卡片检测：对灰度图做局部对比增强，提取连通域后依次检查面积占比、宽高比、框内亮度中位数、框内与环带的对比度、边缘完整性，全部通过才返回卡片框。

（5）数字识别：把卡片内区域归一化为 28×28，按笔画填充率决定膨胀次数以补偿细笔；主模型置信度不足时询问备模型，只有备模型明显更有把握才改判。

（6）播报与日志：播报语句固定为 12 条模板，覆盖成功、看不清、没看到、被拒绝、相机离线、任务进行中等状态；每步写入日志并落盘关键帧，供复盘。"""

OVERALL_RESULT = """系统实物由四足机器人、ZYNQ 开发板、笔记本主机与场地卡片组成。机器人站立状态下，头部相机俯视前方约 1 至 2 m 地面；ZYNQ 板通过网线与主机相连，串接在运动指令路径上。

（实物照片位：系统正面全景、斜 45° 全景）"""

ENG_MECH = """机械部分未作改动，成果体现在场地与视角的工程化：棕色纸壳背景 + 白纸卡片的组合使小尺寸卡片稳定可检；相机俯视角与卡片像素宽度形成稳定的距离—尺寸对应关系，使"走近到可读区间"变成可闭环控制的过程。

（实物照片位：场地布置、卡片与相机视角对照）"""

ENG_CIRCUIT = """电路成果为 ZYNQ 平台上运行的安全闸门。板卡上电后加载含闸门逻辑的可编程逻辑镜像，闸门随服务启动；自检脚本通过 AXI 总线逐项读写寄存器，验证版本号、时钟、看门狗、斜坡、故障闩锁与急停响应。

（实物照片位：开发板整体、芯片与接口特写、自检滚屏）"""

ENG_SOFT = """软件成果包括主机侧语音代理、运动执行器、视觉读数与一组命令行工具。运行时界面为终端日志，逐条打印识别结果、意图、动作、读数与播报内容，便于现场核对。

（界面截图位：语音代理运行日志、读数结果、自检输出）"""

FEATURE_RESULT = """功能与性能逐项实测如下。

（1）安全闸门：27 项检查全部通过，覆盖版本号、时钟比、看门狗超时 200.6 ms、斜坡步数 20 步、故障锁存与急停归零。

（2）转向与直行：45°、45°、-90° 三次转向误差分别为 -1.1°、-0.9°、+1.6°；前进 0.50 m 实测 0.504 m，横向偏差 0.006 m。

（3）扫视与找卡：九个停点覆盖 360°，一轮扫视净漂移 0.137 m；在场地三张卡片中指定目标数字，可自主完成扫视、命中、对准、走近、读数与播报。

（4）数字识别：粗黑马克笔条件下连续 10 次全部正确；标准场景基准 195/200，斜视角压力集 44/52，多墨色集 96.2%。

（5）语音识别：8 段真机音频字错率 2.38%，静音幻觉为 0。

（6）稳定性：全量单元测试 313 项通过；连续运行多次任务无崩溃。

（测试截图位：自检输出、读数结果、基准脚本输出）"""

EXTEND = """后续可从三个方向扩展：其一，补齐绝对定位——当前只有里程计，长时间运行会累积漂移，可引入视觉路标或室内建图实现重定位；其二，把交互从"单张卡片"扩展到"多目标巡检"——一次下达多个点位，自主规划顺序并汇总结果；其三，改善细笔画与遮挡条件下的识别鲁棒性，引入更强的分类器或小样本在线校准。"""

SUMMARY = """这个作品最初的设想只是"让机器狗听懂话并走几步"，真正做下去才发现难点几乎都不在算法本身，而在"接口之间的约定"和"失效时的行为"。

第一个教训来自硬件。安全闸门第一次上板时，斜坡整形没有生效——速度命令一步跳到位，限速形同虚设。查了很久才发现是综合工具把模块参数按字符串下发，"20"被解释成了一个很大的数。修改只有一行，但定位它花了一个晚上。这件事让我记住：硬件行为必须用可观测的寄存器去验证，而不是相信"代码看起来是对的"。

第二个教训来自状态流。转向闭环反复摆动，越纠正越偏，最后发现是状态帧里的航向本来就是角度，而代码又换算了一次，误差被放大了五十七倍。改完那一行，转向误差立刻从几十度收敛到一度多。同一类错误还出现过第二次：停止后发出的第一条运动指令会被丢掉，原因是机器人把控制权判给了"最后一个动作方"，我们必须在每次动作前重新声明控制权。

第三个教训来自视觉。我们一度以为识别率低是模型不行，还认真做过旋转增强和多视角融合实验，结果基准集反而下降。真正的原因朴素得多：卡片在画面里只有一百来像素宽，笔画又细，分类器只能"自信地猜错"。把距离控制好、把笔画加粗、把模糊帧丢掉之后，同一张卡片从零次读对变成十次全对。

这些经历让我对"能演示"和"能工作"的区别有了具体的认识：演示只需要成功一次，工作需要在失败时也知道自己在失败。所以最后阶段我们做的最重要的一件事，不是提高上限，而是降低错误的下限——让系统在读不准的时候说"我看不清"，在停不下来的时候由硬件把速度归零。"""

REFS = """[1] 拉扎维. 模拟 CMOS 集成电路设计[M]. 西安交通大学出版社, 2003.
[2] 王诚, 薛小刚, 钟信潮. FPGA 设计指南[M]. 人民邮电出版社, 2009.
[3] 高翔, 张涛. 视觉 SLAM 十四讲[M]. 电子工业出版社, 2019.
[4] 俞栋, 邓力. 语音识别[M]. 电子工业出版社, 2018.
[5] Xilinx. Zynq-7000 SoC Technical Reference Manual (UG585)[Z]. 2018.
[6] Xilinx. Vivado Design Suite User Guide: Logic Simulation (UG900)[Z]. 2020.
[7] Xilinx. AXI Reference Guide (UG761)[Z]. 2012.
[8] PYNQ Project. PYNQ Documentation[EB/OL]. http://pynq.readthedocs.io.
[9] Pedregosa F, et al. Scikit-learn: Machine Learning in Python[J]. JMLR, 2011, 12: 2825-2830.
[10] Virtanen P, et al. SciPy 1.0: fundamental algorithms for scientific computing in Python[J]. Nature Methods, 2020, 17: 261-272.
[11] LeCun Y, Cortes C. MNIST handwritten digit database[DB/OL]. http://yann.lecun.com/exdb/mnist/.
[12] 中国电子学会. 机器人分类与术语[S]. 2019.
[13] 全国大学生嵌入式芯片与系统设计竞赛组委会. 参赛作品技术文档撰写规范[Z]. 2026.
[14] Bradski G. The OpenCV Library[J]. Dr. Dobb's Journal of Software Tools, 2000.
[15] 需阳科技. 绝影 Lite3 运动主机通讯接口 V1.0.6[Z]. 2025."""


def clear_body(doc):
    body = doc.element.body
    for child in list(body):
        if child.tag.endswith("}sectPr"):
            continue
        body.remove(child)


def add_par(doc, text, style=None, bold_leading=None):
    p = doc.add_paragraph(style=style)
    if bold_leading:
        r = p.add_run(bold_leading)
        r.bold = True
        p.add_run(text)
    else:
        p.add_run(text)
    return p


def add_heading(doc, text, level):
    """模板只有 Heading 1/2，三级标题用加粗段落模拟。"""
    if level <= 2:
        return doc.add_heading(text, level=level)
    p = doc.add_paragraph()
    run = p.add_run(text)
    run.bold = True
    run.font.size = Pt(11)
    p.paragraph_format.space_before = Pt(6)
    p.paragraph_format.space_after = Pt(3)
    return p


def _set_table_borders(table):
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    tbl_pr = table._tbl.tblPr
    borders = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        el = OxmlElement("w:" + edge)
        el.set(qn("w:val"), "single")
        el.set(qn("w:sz"), "4")
        el.set(qn("w:space"), "0")
        el.set(qn("w:color"), "808080")
        borders.append(el)
    tbl_pr.append(borders)


def add_table(doc, rows):
    table = doc.add_table(rows=1, cols=3)
    _set_table_borders(table)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    hdr = table.rows[0].cells
    for i, name in enumerate(("项目", "测试条件", "结果")):
        hdr[i].text = ""
        run = hdr[i].paragraphs[0].add_run(name)
        run.bold = True
    for row in rows:
        cells = table.add_row().cells
        for i, val in enumerate(row):
            cells[i].text = val
    for row in table.rows:
        for cell in row.cells:
            for p in cell.paragraphs:
                p.alignment = WD_ALIGN_PARAGRAPH.LEFT
                for r in p.runs:
                    r.font.size = Pt(9)
    return table

EVIDENCE_INTRO = (
    "本项目的结论不以文字承诺为准，而以程序自己输出的日志、CSV 与自检结果为准。"
    "全部原始文件随代码包一起提交，位于代码包的 提交/证据/ 目录；下表给出结论、复现方式与对应文件。"
)

EVIDENCE_ROWS = [
    ["ZYNQ 安全闸门 27 项自检全部通过（看门狗 201.1 ms、斜坡 20 mm/s 每毫秒）",
     "板上 sudo systemctl stop fpga-gate-bridge && run.sh gate_test.py",
     "证据/fpga/gate_test_20261101.txt"],
    ["每段运动都经过闸门：一次演示中 42 次开门 / 42 次关门，无例外",
     "板上 grep -c 'gate enabled' pl_service.log",
     "证据/fpga/pl_service_run_20261101.log"],
    ["上位机失联时闸门自行关闭：硬件闩锁计数 1 -> 2 -> 3（粘性）",
     "python _scratch/deadman_test.py（发 0.3 s 命令后不发停止帧）",
     "证据/fpga/deadman_20261101.log"],
    ["转向误差 -1.1 / -0.9 / +1.6 度；直行 0.504 m，横向偏差 0.006 m",
     "python tools/turn_check.py --turns 45,45,-90 --point 0.5,0 --rearm 0x21010C03 --approve",
     "证据/motion/turn_check_20261027T204625.csv"],
    ["同一张卡连续 10 次读数 10/10 正确（置信度 0.77-0.81）",
     "python tools/digit_live.py --shots 3（同一位置重复 10 次）",
     "证据/vision/read10_*.csv"],
    ["端到端语音链路：听到\"小狗去八号\" -> 转圈找到 8（0.9937）-> 走近到 174 px -> 读出 8（0.991）-> 播报",
     "python tools/voice_agent.py --allow-motion，然后说该句话",
     "证据/voice/voice_agent_hunt8_20261101_152539.log"],
    ["314 项单元测试全部通过", "python -m pytest tests/ -q", "证据/tests/pytest_20261101.txt"],
]

EVIDENCE_NOTES = [
    "1) 闸门自检会重载 overlay，因此必须先把 fpga-gate-bridge 服务停掉再跑，跑完再启动。",
    "2) 板端时钟此前未校准（显示 2021 年），采集证据前已用 sudo date -s 与主机对齐；旧日志中的 2021 时间戳按 主机时间 = 板端时间 - 1 小时 34 分 34 秒 换算。",
    "3) deadman 补测时狗不处于可运动状态，因此该次测试只验证闸门侧行为：上位机停止发送后 300 ms 软件关闸、200 ms 硬件看门狗归零，狗未收到任何有效速度。",
    "4) 本次提交以代码、日志与文档为证，未录制演示视频；所有实测条件（距离、光照、字号、电量）记录在 docs/progress.md 与各 runbook 中，便于按同一条件复跑。",
]


def main(out_path):
    doc = Document(str(TEMPLATE))
    clear_body(doc)

    add_heading(doc, TITLE, 1)
    sub = doc.add_paragraph()
    run = sub.add_run("（正文中不含学校、指导教师等与作品无关的信息）")
    run.italic = True
    run.font.size = Pt(9)

    add_heading(doc, "摘要", 2)
    add_par(doc, ABSTRACT)

    add_heading(doc, "第一部分  作品概述", 1)
    add_heading(doc, "功能与特性", 2)
    add_par(doc, FUNC)
    add_heading(doc, "应用领域", 2)
    add_par(doc, APPLY)
    add_heading(doc, "主要技术特点", 2)
    add_par(doc, TECH)
    add_heading(doc, "主要性能指标", 2)
    add_par(doc, "主要指标均在真实硬件上测得，测试方法见第三部分。")
    add_table(doc, PERF_ROWS)
    add_heading(doc, "主要创新点", 2)
    add_par(doc, INNOV)
    add_heading(doc, "设计流程", 2)
    add_par(doc, FLOW)

    add_heading(doc, "第二部分  系统组成及功能说明", 1)
    add_heading(doc, "整体介绍", 2)
    add_par(doc, OMNI)
    add_heading(doc, "硬件系统介绍", 2)
    add_heading(doc, "2.2.1 硬件整体介绍", 3)
    add_par(doc, HW_OVERALL)
    add_heading(doc, "2.2.2 机械设计介绍", 3)
    add_par(doc, MECH)
    add_heading(doc, "2.2.3 电路各模块介绍", 3)
    add_par(doc, CIRCUIT)
    add_heading(doc, "软件系统介绍", 2)
    add_heading(doc, "2.3.1 软件整体介绍", 3)
    add_par(doc, SW_OVERALL)
    add_heading(doc, "2.3.2 软件各模块介绍", 3)
    add_par(doc, SW_MODULES)

    add_heading(doc, "第三部分  完成情况及性能参数", 1)
    add_heading(doc, "整体介绍", 2)
    add_par(doc, OVERALL_RESULT)
    add_heading(doc, "工程成果", 2)
    add_heading(doc, "3.2.1 机械成果", 3)
    add_par(doc, ENG_MECH)
    add_heading(doc, "3.2.2 电路成果", 3)
    add_par(doc, ENG_CIRCUIT)
    add_heading(doc, "3.2.3 软件成果", 3)
    add_par(doc, ENG_SOFT)
    add_heading(doc, "特性成果", 2)
    add_par(doc, FEATURE_RESULT)

    add_heading(doc, "第四部分  总结", 1)
    add_heading(doc, "可扩展之处", 2)
    add_par(doc, EXTEND)
    add_heading(doc, "心得体会", 2)
    add_par(doc, SUMMARY)

    add_heading(doc, "第五部分  参考文献", 1)
    for line in REFS.strip().splitlines():
        p = doc.add_paragraph(line)
        p.paragraph_format.space_after = Pt(2)

    add_heading(doc, "附录 A  证据清单（每条结论都可复跑）", 1)
    add_par(doc, EVIDENCE_INTRO)
    add_table(doc, [["结论", "复现命令", "证据文件"]] + EVIDENCE_ROWS)
    add_heading(doc, "附录 B  证据文件说明", 2)
    for line in EVIDENCE_NOTES:
        add_par(doc, line)

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out))
    print("saved:", out)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_OUT)
