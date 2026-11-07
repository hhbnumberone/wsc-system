# -*- coding: utf-8 -*-
"""单元测试。运行：python -m unittest discover -s tests -v

测试重点放在**状态机与不变量**上，不测界面。原因：
状态机的正确性不依赖 UI，而 UI 依赖状态机。先把"不丢不重"钉死，
界面怎么改都不会把数据搞坏。
"""
# 日志压制见 common/logging_setup.quiet()，由各测试模块显式调用。
# 不放在这里：`discover -s tests` 把测试模块按顶层模块名加载，
# 这个包的 __init__ 根本不会被执行。
