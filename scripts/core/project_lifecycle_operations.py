"""One declaration for project creation, backup and restore in every transport."""


def project_lifecycle_operations():
    from .operations import OperationSpec, STRING, INTEGER, BOOLEAN, _schema
    from .project_archive import backup_project, commit_restore_project, preview_restore_project, verify_project_backup
    from .project_lifecycle import commit_create_project, preview_create_project
    approval = {'planRef': STRING, 'planHash': STRING, 'approve': {**BOOLEAN, 'const': True}}
    return [
        OperationSpec('project.create-preview', 'preview_project_creation',
            '预览创建当前格式的空小说项目，自动生成项目身份并采用通用结构。目标目录须不存在，预览不创建项目。',
            _schema({'bookName': STRING, 'noAgentsMd': BOOLEAN}, ['projectRoot', 'bookName']),
            'plan-write', lambda root, a: preview_create_project(root, **a), ('create-preview',),
            category='author', discover=True, examples=({'projectRoot': '/path/to/新书', 'bookName': '山城旧事'},)),
        OperationSpec('project.create-commit', 'commit_project_creation',
            '明确批准新建预览后，一次性发布完整空项目。原计划可重试，不覆盖已有项目。',
            _schema(approval, ['projectRoot', 'planRef', 'planHash', 'approve']),
            'protected-write', lambda root, a: commit_create_project(root, **a), ('create-commit',), category='author'),
        OperationSpec('project.backup', 'backup_project',
            '将当前格式项目的全部文件、空目录、批准及事务证据备份到项目外的新 ZIP 文件；检测并发改动，不保存运行中的宿主进程。',
            _schema({'output': STRING, 'expectedRevision': INTEGER}, ['projectRoot', 'output']),
            'export-write', lambda root, a: backup_project(root, **a), ('backup',), category='author', discover=True,
            examples=({'projectRoot': '/path/to/书', 'output': '/path/to/backups/书.zip'},)),
        OperationSpec('project.backup-verify', 'verify_project_backup',
            '逐文件验证完整项目备份的摘要、路径、清单及当前格式，不恢复、不修改备份或项目。',
            _schema({'archive': STRING, 'expectedArchiveHash': STRING}, ['archive']),
            'read', lambda root, a: verify_project_backup(**a), ('backup-verify',), category='author',
            examples=({'archive': '/path/to/backups/书.zip'},)),
        OperationSpec('project.restore-preview', 'preview_project_restore',
            '核验备份并预览恢复到新目录；保留原项目身份与批准证据，明确列出将停止的原宿主交接。',
            _schema({'archive': STRING, 'expectedArchiveHash': STRING}, ['projectRoot', 'archive']),
            'plan-write', lambda root, a: preview_restore_project(root, **a), ('restore-preview',),
            category='author', discover=True,
            examples=({'projectRoot': '/path/to/恢复项目', 'archive': '/path/to/backups/书.zip'},)),
        OperationSpec('project.restore-commit', 'commit_project_restore',
            '批准恢复预览后在新目录发布完整项目，追加定位历史并保留原元数据副本；不重放活跃宿主任务，不新增章节批准。',
            _schema(approval, ['projectRoot', 'planRef', 'planHash', 'approve']),
            'protected-write', lambda root, a: commit_restore_project(root, **a), ('restore-commit',), category='author'),
    ]
