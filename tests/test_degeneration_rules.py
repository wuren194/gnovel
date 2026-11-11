"""Generation-defect controls independent of any private manuscript."""
import unittest

from scripts.check_degeneration import detect_ai_self_reference, detect_repetitions, scan


class DegenerationRulesTests(unittest.TestCase):
    def test_short_loops_are_reported_without_blocking(self):
        sentence = '远处那座小钟楼依旧亮着灯。'
        for separator in ('', '\n', '\n\n'):
            with self.subTest(separator=separator):
                report = scan(separator.join([sentence] * 3))
                self.assertEqual(report['blocking_count'], 0)
                self.assertEqual(report['recommendation'], 'review_advisory')
                self.assertEqual([row['pattern'] for row in report['advisory']], ['短句连续复读'])
        self.assertFalse(detect_repetitions(sentence * 2))
        self.assertFalse(detect_repetitions('再等等。' * 3))
        self.assertFalse(detect_repetitions(('“' + sentence + '”\n') * 3))

    def test_long_sentences_repeated_inside_one_line_are_detected(self):
        sentence = '值班员沿着走廊来到仓库门前核对过登记表以后才放下手里的旧木箱。'
        report = scan('清晨。' + sentence * 3)
        self.assertEqual([row['pattern'] for row in report['blocking']], ['逐字复读'])
        self.assertEqual(report['blocking'][0]['line'], 1)
        self.assertFalse(detect_repetitions(('“' + sentence + '”\n') * 3))

    def test_first_person_limits_and_apologies_are_not_assistant_refusals(self):
        for text in ('我不能及时赶到码头，只好改乘明天的船。', '我无法辨认落款，拿来一盏灯。',
                     '很抱歉，那件行李送错了。', '我理解他为何放弃。',
                     '作为人工智能时代的产物，展品用的是旧式电池。'):
            with self.subTest(text=text):
                self.assertFalse(detect_ai_self_reference(text))

    def test_explicit_generation_leaks_and_trailing_narration_remain_visible(self):
        for text in ('作为一名AI助手，我无法继续生成这段内容。', '作为一个语言模型，我无法续写正文。',
                     '我不能继续生成这段内容。', '让我们来看一下接下来的情节发展。',
                     '“信收到了。”作为一个AI，我无法生成正文。'):
            with self.subTest(text=text):
                self.assertTrue(detect_ai_self_reference(text))
        self.assertFalse(detect_ai_self_reference('录音里说：“作为一个AI，\n我无法生成正文。”'))

    def test_metadata_is_not_prose_and_cannot_hide_a_truncated_body(self):
        content = '作为一个AI，我无法生成正文。\nContextPack\n未完的句子'
        for wrapper in ('```text\n{}\n```', '~~~text\n{}\n~~~', '---\n{}\n---', '<!-- {} -->'):
            with self.subTest(wrapper=wrapper):
                self.assertEqual(scan(wrapper.format(content))['total_count'], 0)
        report = scan('她刚刚打开信封\n\n<!-- 完 -->')
        self.assertEqual([row['pattern'] for row in report['blocking']], ['末尾截断'])
        self.assertEqual(report['blocking'][0]['line'], 1)
        self.assertEqual(scan('第一章 寄信\n\n她寄出了信。')['total_count'], 0)


if __name__ == '__main__':
    unittest.main()
