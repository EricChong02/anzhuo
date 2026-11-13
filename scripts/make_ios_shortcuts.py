"""生成 iOS 远窗入口、私人分享和截图快捷指令；令牌只写入分享成品，不进源码。"""
from __future__ import annotations
import argparse
import json
import plistlib
import uuid
from pathlib import Path
from urllib.parse import urlsplit


def attachment(value):
    return {"Value": value, "WFSerializationType": "WFTextTokenAttachment"}


def token_text(value):
    return {"Value": {"string": "\ufffc", "attachmentsByRange": {"{0, 1}": value}},
            "WFSerializationType": "WFTextTokenString"}


def plain_text(value):
    return {"Value": {"string": value, "attachmentsByRange": {}}, "WFSerializationType": "WFTextTokenString"}


def desktop_url(base, fragment=''):
    """只唤起已安装的主屏幕远窗；HTTPS 留给后台请求，不作 Safari 降级。"""
    parts = urlsplit(base)
    path = parts.path.rstrip('/') + '/_x/'
    return 'webapp://' + parts.netloc + path + ('#' + fragment if fragment else '')


def build_launcher(base):
    """操作按钮唤起主屏幕远窗，并请求打开新任务页。"""
    identifier = str(uuid.uuid4()).upper()
    url = desktop_url(base, 'new')
    return {
        'WFWorkflowName': '交代远窗',
        'WFWorkflowClientVersion': '3036.0.4.2',
        'WFWorkflowMinimumClientVersion': 900,
        'WFWorkflowMinimumClientVersionString': '900',
        'WFWorkflowIcon': {'WFWorkflowIconStartColor': 4282601983, 'WFWorkflowIconGlyphNumber': 59511},
        'WFWorkflowTypes': [],
        'WFWorkflowInputContentItemClasses': [],
        'WFWorkflowOutputContentItemClasses': [],
        'WFWorkflowImportQuestions': [],
        'WFWorkflowActions': [
            {'WFWorkflowActionIdentifier': 'is.workflow.actions.url',
             'WFWorkflowActionParameters': {'UUID': identifier, 'WFURLActionURL': url}},
            {'WFWorkflowActionIdentifier': 'is.workflow.actions.openurl',
             'WFWorkflowActionParameters': {'WFInput': attachment({'Type': 'ActionOutput',
                 'OutputUUID': identifier, 'OutputName': 'URL'})}},
        ],
    }


def build(base, token, mode):
    if mode == "launch":
        return build_launcher(base)
    actions = []

    def add(kind, **params):
        identifier = str(uuid.uuid4()).upper()
        params['UUID'] = identifier
        actions.append({'WFWorkflowActionIdentifier': 'is.workflow.actions.' + kind,
                        'WFWorkflowActionParameters': params})
        return {'Type': 'ActionOutput', 'OutputUUID': identifier, 'OutputName': '结果'}

    def variable(name):
        return {'Type': 'Variable', 'VariableName': name}

    def typed(value, item_class):
        # 条件判断须明确输入类型，否则原生编辑器可能留下未填写的比较参数。
        return {**value, 'Aggrandizements': [
            {'Type': 'WFCoercionVariableAggrandizement', 'CoercionItemClass': item_class}]}

    def setvar(name, value):
        add('setvariable', WFVariableName=name, WFInput=attachment(value))

    def literal(name, value):
        setvar(name, add('gettext', WFTextActionText=value))

    def getkey(source, key):
        return add('getvalueforkey', WFInput=attachment(source), WFGetDictionaryValueType='Value', WFDictionaryKey=key)

    def choose(values, prompt):
        source = add('list', WFItems=values)
        return add('choosefromlist', WFInput=attachment(source), WFChooseFromListActionPrompt=prompt,
                   WFChooseFromListActionSelectMultiple=False)

    def group():
        return str(uuid.uuid4()).upper()

    def branch(g, title):
        add('choosefrommenu', WFMenuItemTitle=title, WFControlFlowMode=1, GroupingIdentifier=g)

    def composed(parts):
        text, refs = '', {}
        for part in parts:
            if isinstance(part, str):
                text += part
            else:
                offset = len(text.encode('utf-16-le')) // 2
                refs['{%d, 1}' % offset] = part
                text += '\ufffc'
        return {'Value': {'string': text, 'attachmentsByRange': refs}, 'WFSerializationType': 'WFTextTokenString'}

    names = {'share': '分享给远窗', 'voice': '语音交给远窗', 'screenshot': '截图交给远窗'}
    add('comment', WFCommentActionText='私人指令，请勿转发。先交代事情，再确认发送；只有选择“发送”才上传。取消会退出。已有对话沿用原模型。')
    shared = {'Type': 'ExtensionInput'}
    # 操作按钮路径先听用户说话，不先联网或弹设置菜单。
    if mode == 'voice':
        text = add('dictatetext', WFDictateTextLanguage='zh-CN', WFDictateTextStopListening='On Tap')
    else:
        if mode == 'screenshot':
            shared = add('selectphoto', WFSelectMultiplePhotos=True)
        text = add('ask', WFAskActionPrompt='要远窗做什么？', WFInputType='Text',
                   WFAskActionDefaultAnswer='请根据截图排查原因并给出操作步骤' if mode == 'screenshot' else '')
    setvar('要求', text)
    literal('目标', '新开对话')
    literal('模型', '自动选择')
    literal('任务', '排查截图问题' if mode == 'screenshot' else '自定义要求')
    endpoint = base.rstrip('/') + '/_x/q/' + token
    repeat = group()
    add('repeat.count', WFRepeatCount=20, WFControlFlowMode=0, GroupingIdentifier=repeat)
    condition = group()
    add('conditional', WFInput={'Type': 'Variable', 'Variable': attachment(typed(variable('目标'), 'WFStringContentItem'))}, WFCondition=4,
        WFConditionalActionString=plain_text('新开对话'), WFControlFlowMode=0, GroupingIdentifier=condition)
    setvar('模型说明', variable('模型'))
    add('conditional', WFControlFlowMode=1, GroupingIdentifier=condition)
    literal('模型说明', '沿用原对话模型')
    add('conditional', WFControlFlowMode=2, GroupingIdentifier=condition)
    menu = group()
    items = ['发送', '修改要求', '更换模型', '补充已有任务／新开', '选择任务模板']
    prompt = composed(['要求：', variable('要求'), '\n\n去向：', variable('目标'),
                       '\n模型：', variable('模型说明'), '\n任务：', variable('任务')])
    add('choosefrommenu', WFMenuPrompt=prompt, WFMenuItems=items, WFControlFlowMode=0, GroupingIdentifier=menu)
    branch(menu, '发送')
    fields = []
    for key, name in [('text', '要求'), ('target', '目标'), ('template', '任务'), ('model', '模型')]:
        fields.append({'WFItemType': 0, 'WFKey': plain_text(key), 'WFValue': token_text(variable(name))})
    if mode != 'voice':
        fields.append({'WFItemType': 5, 'WFKey': plain_text('file'), 'WFValue': {
            'Value': attachment(shared), 'WFSerializationType': 'WFTokenAttachmentParameterState'}})
    form = {'Value': {'WFDictionaryFieldValueItems': fields}, 'WFSerializationType': 'WFDictionaryFieldValue'}
    params = dict(WFURL=endpoint, WFHTTPMethod='POST', WFHTTPBodyType='Form', WFFormValues=form)
    if mode != 'voice':
        params['WFRequestVariable'] = attachment(shared)
    result = add('downloadurl', **params)
    message = getkey(result, 'message')
    ok = getkey(result, 'ok')
    condition = group()
    add('conditional', WFInput={'Type': 'Variable', 'Variable': attachment(typed(ok, 'WFNumberContentItem'))}, WFCondition=4,
        WFNumberValue=1, WFControlFlowMode=0, GroupingIdentifier=condition)
    num = getkey(result, 'num')
    url = add('url', WFURLActionURL=composed([desktop_url(base, 's'), num]))
    receipt = group()
    add('choosefrommenu', WFMenuPrompt=token_text(message), WFMenuItems=['查看对话', '完成'],
        WFControlFlowMode=0, GroupingIdentifier=receipt)
    branch(receipt, '查看对话')
    add('openurl', WFInput=attachment(url))
    branch(receipt, '完成')
    add('nothing')
    add('choosefrommenu', WFControlFlowMode=2, GroupingIdentifier=receipt)
    add('conditional', WFControlFlowMode=1, GroupingIdentifier=condition)
    add('alert', WFAlertActionTitle='尚未发送成功', WFAlertActionMessage=token_text(message), WFAlertActionCancelButtonShown=False)
    add('conditional', WFControlFlowMode=2, GroupingIdentifier=condition)
    add('exit', WFResult=attachment(result))
    branch(menu, '修改要求')
    edited = add('ask', WFAskActionPrompt='修改后回到确认页', WFInputType='Text',
                 WFAskActionDefaultAnswer=token_text(variable('要求')))
    setvar('要求', edited)
    branch(menu, '更换模型')
    condition = group()
    add('conditional', WFInput={'Type': 'Variable', 'Variable': attachment(typed(variable('目标'), 'WFStringContentItem'))}, WFCondition=4,
        WFConditionalActionString=plain_text('新开对话'), WFControlFlowMode=0, GroupingIdentifier=condition)
    model = choose(['自动选择', 'Claude Opus', 'Claude Sonnet', 'Claude Haiku', 'GPT'], '新对话使用哪个模型？')
    setvar('模型', model)
    add('conditional', WFControlFlowMode=1, GroupingIdentifier=condition)
    add('alert', WFAlertActionTitle='沿用已有模型', WFAlertActionMessage='补充已有任务会沿用原模型。要换模型，请先在“补充已有任务／新开”中选择“新开对话”。', WFAlertActionCancelButtonShown=False)
    add('conditional', WFControlFlowMode=2, GroupingIdentifier=condition)
    branch(menu, '补充已有任务／新开')
    options = add('downloadurl', WFURL=endpoint + '/options', WFHTTPMethod='GET')
    target = add('choosefromlist', WFInput=attachment(getkey(options, 'targets')),
                 WFChooseFromListActionPrompt='发到哪里？', WFChooseFromListActionSelectMultiple=False)
    setvar('目标', target)
    branch(menu, '选择任务模板')
    template = choose(['自定义要求', '总结＋待办', '帮我拟回复', '整理成工作记录', '排查截图问题'], '需要什么结果？')
    setvar('任务', template)
    add('choosefrommenu', WFControlFlowMode=2, GroupingIdentifier=menu)
    add('repeat.count', WFControlFlowMode=2, GroupingIdentifier=repeat)
    add('alert', WFAlertActionTitle='尚未发送', WFAlertActionMessage='本次没有发送内容，请重新运行后确认发送。', WFAlertActionCancelButtonShown=False)
    shortcut = {'WFWorkflowName': names[mode], 'WFWorkflowClientVersion': '3036.0.4.2',
                'WFWorkflowMinimumClientVersion': 900, 'WFWorkflowMinimumClientVersionString': '900',
                'WFWorkflowIcon': {'WFWorkflowIconStartColor': 4282601983, 'WFWorkflowIconGlyphNumber': 59511},
                'WFWorkflowTypes': ['ActionExtension'] if mode == 'share' else [],
                'WFWorkflowInputContentItemClasses': ['WFGenericFileContentItem', 'WFImageContentItem', 'WFAVAssetContentItem',
                                                     'WFPDFContentItem', 'WFStringContentItem', 'WFURLContentItem'],
                'WFWorkflowOutputContentItemClasses': [], 'WFWorkflowImportQuestions': [], 'WFWorkflowActions': actions}
    if mode == 'share':
        shortcut['WFWorkflowNoInputBehavior'] = {'Name': 'StopWithError', 'Parameters': {
            'ErrorMessage': '请先选中文件或照片，再通过分享菜单运行“分享给远窗”。直接交代任务请用“交代远窗”。'}}
    return shortcut


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', required=True, help='远窗 HTTPS 地址')
    parser.add_argument('--token-file', type=Path, default=Path.home() / '.local/share/anzhuo/shortcut.json')
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    if not args.base.startswith('https://'):
        parser.error('私人凭据只能通过 HTTPS 发送')
    token = json.loads(args.token_file.read_text())['token']
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for mode in ['launch', 'share', 'screenshot']:
        data = build(args.base, token, mode)
        path = args.output_dir / (data['WFWorkflowName'] + '.unsigned.shortcut')
        path.write_bytes(plistlib.dumps(data, fmt=plistlib.FMT_BINARY))
        path.chmod(0o600)
        print(path)


if __name__ == '__main__':
    main()
