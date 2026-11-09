"""Current ownership docs preserve protection, not retired migration section labels.

Runtime adversarial coverage lives in ownership and undo suites. These checks
keep the active contract self-contained and separate from historical authority.
"""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT/'docs/R1-CURRENT-OWNERSHIP-BINDING.md'


class CurrentOwnershipContractTests(unittest.TestCase):
    def setUp(self):
        self.text = CONTRACT.read_text(encoding='utf-8')

    def section(self, title):
        return self.text.split('## '+title+'\n', 1)[1].split('\n## ', 1)[0]

    def test_current_authority_is_not_filename_hash_or_history(self):
        authority = self.section('当前权威与不变量')
        for value in ('chapterId', 'filePath', 'proseBinding', '盘上观测',
                      '不能单独证明归属', '不能自动接管', '不依赖历史日志'):
            self.assertIn(value, authority)
        self.assertIn('其他格式不参与当前归属判定', self.text)
        self.assertIn('不能继承旧身份或批准', self.text)

    def test_unresolved_reserves_locator_but_never_grants_write(self):
        reservations = self.section('位置保留与故障隔离')
        for value in ('unresolved', '排他保留', '物理文件缺失不释放位置',
                      '不等于取得正向归属', 'chapter-local', 'project-fatal'):
            self.assertIn(value, reservations)
        self.assertIn('unresolved 不允许普通 move、rename 或 finalize', self.text)
        self.assertIn('纯大小写和 Unicode 规范化形式改名', self.text)
        self.assertIn('不通过中间文件名绕过', self.text)

    def test_integrity_never_silently_reseals(self):
        binding = self.section('持久绑定与完整性')
        self.assertIn('seal_binding()', binding)
        self.assertIn('不是数字签名', binding)
        self.assertIn('不能重封损坏 binding', binding)
        self.assertIn('OWNERSHIP_BINDING_INVALID', binding)

    def test_materialization_authority_and_revision_are_exact(self):
        writers = self.section('通用写者边界')
        for value in ('通用 `save_structure`', '也不得新增正式章节',
                      '候选不得自带 proseBinding',
                      '`generation = proposed.revision`',
                      '`proposed.revision == current.revision + 1`',
                      '精确等于实际新增 chapterId 集合', '实际受保护变更集合'):
            self.assertIn(value, writers)

    def test_formal_undo_restores_only_changed_semantics_and_never_old_token(self):
        undo = self.section('代次、撤销与恢复')
        for value in ('只恢复该操作实际改变的 binding 语义字段',
                      '绝不恢复旧 generation 或 integrityDigest',
                      '不为章节无理由推进 generation', 'UNDO_NOT_CHAIN_TOP',
                      '服务端不能在缺令牌时自行补当前值', '避免 ABA 复活'):
            self.assertIn(value, undo)

    def test_transaction_paths_and_prepared_recovery_exception_are_explicit(self):
        recovery = self.section('代次、撤销与恢复')
        for path in ('.govel/transactions/<TX-ID>.json',
                     '.govel/transactions/<TX-ID>/before/',
                     '.govel/transactions/<TX-ID>/after/'):
            self.assertIn(path, recovery)
        for value in ('`phase != committed`', '`phase == committed`', 'COMPLETE OLD', 'COMPLETE NEW',
                      '未提交事务恢复可恢复当时 revision',
                      '不新增 `prepared → forward replay`', '不能成为恢复写入目标'):
            self.assertIn(value, recovery)

    def test_external_accept_keeps_capability_separation(self):
        transitions = self.section('受控流转')
        for value in ('只允许 preview', '独立特权批准能力', 'APPROVAL_CAPABILITY_REQUIRED',
                      'actor 字符串', '重算 planHash', '均不是该能力'):
            self.assertIn(value, transitions)
        self.assertIn('提交在锁内重新验证目标、代次、工件和批准', self.text)

    def test_contract_has_no_current_native_migration_or_later_override(self):
        for obsolete in ('ownership-migration', 'schema-upgrade', 'Cutover Migration',
                         '冲突处以本章为准'):
            self.assertNotIn(obsolete, self.text)
        historic = ROOT/'docs/archive/pre-13-current-format/docs/R1-CURRENT-OWNERSHIP-BINDING.md'
        self.assertFalse(historic.exists(), 'historical implementations are not part of this distribution')


if __name__ == '__main__':
    unittest.main()
