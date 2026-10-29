-- ============================================================
-- OTHVM — Seed Data for core_context and othman_memory
-- Run this AFTER 001_create_tables.sql
-- ============================================================

-- ============================================================
-- Populate core_context with structure
-- The founder should update the values below
-- ============================================================
INSERT INTO core_context (founder_name, founder_summary, othvm_definition, othvm_capabilities, last_updated)
VALUES (
    '{{FOUNDER_NAME — أدخل اسمك الكامل هنا}}',
    '{{FOUNDER_SUMMARY — اكتب ملخصاً قصيراً عن نفسك: من أنت، ماذا تعمل، ما اهتماماتك}}',
    'OTHVM هو نظام تشغيل شخصي بالذكاء الاصطناعي مبني على n8n. يعمل كمساعد ذكي شامل يتحكم في الجدول اليومي، البريد، المهام، المشاريع، والتحليل الأسبوعي. يتواصل حصرياً عبر تيليجرام ويستخدم اللغة العربية.',
    'إدارة التقويم والمواعيد | إدارة البريد الإلكتروني | إدارة المهام | متابعة المشاريع | تنبيهات الطقس | تذكير الصلوات | تحليل أسبوعي | ملخص صباحي يومي | مراقبة GitHub | البحث في جوجل | إدارة Google Drive و Docs و Sheets',
    now()
)
ON CONFLICT DO NOTHING;

-- ============================================================
-- Populate othman_memory with full category/key structure
-- ============================================================

-- Category: personal_info
INSERT INTO othman_memory (category, key, value) VALUES
    ('personal_info', 'full_name', '{{أدخل اسمك الكامل}}'),
    ('personal_info', 'nickname', '{{أدخل اسمك المختصر أو لقبك}}'),
    ('personal_info', 'location_city', '{{أدخل مدينتك}}'),
    ('personal_info', 'location_governorate', '{{أدخل المحافظة أو الولاية}}'),
    ('personal_info', 'location_country', '{{أدخل بلدك}}'),
    ('personal_info', 'location_coordinates', '{{أدخل خط العرض، خط الطول}}'),
    ('personal_info', 'timezone', '{{أدخل منطقتك الزمنية — مثال: Africa/Cairo}}'),
    ('personal_info', 'email', '{{أدخل بريدك الإلكتروني الأساسي}}'),
    ('personal_info', 'phone', '{{أدخل رقم هاتفك}}'),
    ('personal_info', 'telegram_chat_id', '{{TELEGRAM_CHAT_ID — أدخل معرف محادثتك في تيليجرام}}'),
    ('personal_info', 'occupation', '{{أدخل مهنتك أو مجال عملك}}'),
    ('personal_info', 'birthday', '{{أدخل تاريخ ميلادك}}')
ON CONFLICT (category, key) DO NOTHING;

-- Category: preferences
INSERT INTO othman_memory (category, key, value) VALUES
    ('preferences', 'language', 'العربية'),
    ('preferences', 'time_format', '12h — صباحاً / مساءً'),
    ('preferences', 'communication_style', '{{هل تفضل الرسائل المختصرة أم المفصلة؟}}'),
    ('preferences', 'morning_brief_time', '{{متى تريد الملخص الصباحي؟ مثال: 07:00}}'),
    ('preferences', 'notification_level', '{{مستوى الإشعارات: عالي / متوسط / منخفض}}'),
    ('preferences', 'weather_units', 'metric — درجة مئوية'),
    ('preferences', 'important_email_senders', '{{قائمة بأهم المرسلين الذين يجب عدم تجاهل رسائلهم، مفصولة بفاصلة}}')
ON CONFLICT (category, key) DO NOTHING;

-- Category: daily_patterns
INSERT INTO othman_memory (category, key, value) VALUES
    ('daily_patterns', 'wake_up_time', '{{متى تستيقظ عادة؟}}'),
    ('daily_patterns', 'sleep_time', '{{متى تنام عادة؟}}'),
    ('daily_patterns', 'work_hours', '{{ساعات عملك — مثال: 9 صباحاً - 5 مساءً}}'),
    ('daily_patterns', 'peak_productivity', '{{أوقات ذروة إنتاجيتك}}'),
    ('daily_patterns', 'exercise_time', '{{وقت التمرين إن وجد}}'),
    ('daily_patterns', 'prayer_method', '{{أدخل طريقة حساب مواقيت الصلاة}}'),
    ('daily_patterns', 'fajr_time', '{{يتم حسابه تلقائياً}}'),
    ('daily_patterns', 'dhuhr_time', '{{يتم حسابه تلقائياً}}'),
    ('daily_patterns', 'asr_time', '{{يتم حسابه تلقائياً}}'),
    ('daily_patterns', 'maghrib_time', '{{يتم حسابه تلقائياً}}'),
    ('daily_patterns', 'isha_time', '{{يتم حسابه تلقائياً}}')
ON CONFLICT (category, key) DO NOTHING;

-- Category: personal_context
INSERT INTO othman_memory (category, key, value) VALUES
    ('personal_context', 'current_focus', '{{ما الذي تركز عليه حالياً؟}}'),
    ('personal_context', 'current_challenges', '{{ما التحديات الحالية؟}}'),
    ('personal_context', 'short_term_goals', '{{أهدافك قصيرة المدى}}'),
    ('personal_context', 'long_term_goals', '{{أهدافك طويلة المدى}}'),
    ('personal_context', 'learning_interests', '{{ما الذي تتعلمه حالياً؟}}')
ON CONFLICT (category, key) DO NOTHING;

-- ============================================================
-- Populate othvm_memory with system knowledge
-- ============================================================

-- Category: capabilities
INSERT INTO othvm_memory (category, key, value) VALUES
    ('capabilities', 'email_management', 'قراءة وتصنيف وإرسال البريد الإلكتروني عبر Gmail'),
    ('capabilities', 'calendar_management', 'إدارة الأحداث والمواعيد عبر Google Calendar'),
    ('capabilities', 'task_management', 'إدارة المهام عبر Google Tasks و Notion'),
    ('capabilities', 'document_management', 'إنشاء وتحرير المستندات عبر Google Docs و Sheets'),
    ('capabilities', 'file_management', 'إدارة الملفات عبر Google Drive'),
    ('capabilities', 'weather_info', 'استعلام عن الطقس الحالي والتوقعات'),
    ('capabilities', 'github_monitoring', 'متابعة Issues و Pull Requests'),
    ('capabilities', 'contact_management', 'إدارة جهات الاتصال عبر Google Contacts'),
    ('capabilities', 'youtube_search', 'البحث في YouTube'),
    ('capabilities', 'web_search', 'البحث في جوجل'),
    ('capabilities', 'notion_management', 'إدارة صفحات وقواعد بيانات Notion'),
    ('capabilities', 'memory_system', 'حفظ واسترجاع المعلومات من نظام الذاكرة في Supabase')
ON CONFLICT (category, key) DO NOTHING;

-- Category: workflows
INSERT INTO othvm_memory (category, key, value) VALUES
    ('workflows', 'morning_brief', 'ملخص صباحي يومي: تقويم اليوم + المهام المعلقة + الطقس'),
    ('workflows', 'prayer_reminders', 'تذكير بأوقات الصلاة 5 مرات يومياً'),
    ('workflows', 'weather_alert', 'تنبيه فوري عند ظروف جوية قاسية'),
    ('workflows', 'calendar_check', 'فحص كل ساعتين للمواعيد القادمة خلال 3 ساعات'),
    ('workflows', 'gmail_check', 'فحص صباحي للرسائل المهمة في آخر 24 ساعة'),
    ('workflows', 'github_check', 'فحص يومي للـ Issues و PRs المفتوحة'),
    ('workflows', 'weather_tomorrow', 'فحص مسائي لطقس الغد مقارنة بأحداث التقويم'),
    ('workflows', 'weekly_analysis', 'تحليل أسبوعي للأنماط وتحديث الذاكرة'),
    ('workflows', 'weekly_review', 'مراجعة أسبوعية شاملة كل أحد مساءً')
ON CONFLICT (category, key) DO NOTHING;

-- Category: connected_tools
INSERT INTO othvm_memory (category, key, value) VALUES
    ('connected_tools', 'gmail', 'متصل — gmailOAuth2'),
    ('connected_tools', 'google_calendar', 'متصل — googleCalendarOAuth2Api'),
    ('connected_tools', 'google_tasks', 'متصل — googleTasksOAuth2Api'),
    ('connected_tools', 'google_sheets', 'متصل — googleSheetsOAuth2Api'),
    ('connected_tools', 'google_docs', 'متصل — googleDocsOAuth2Api'),
    ('connected_tools', 'google_contacts', 'متصل — googleContactsOAuth2Api'),
    ('connected_tools', 'google_drive', 'متصل — googleDriveOAuth2Api'),
    ('connected_tools', 'youtube', 'متصل — youTubeOAuth2Api'),
    ('connected_tools', 'telegram', 'متصل — telegramApi'),
    ('connected_tools', 'supabase', 'متصل — supabaseApi'),
    ('connected_tools', 'notion', 'متصل — notionApi'),
    ('connected_tools', 'github', 'متصل — githubOAuth2Api'),
    ('connected_tools', 'gemini_api', 'متصل — googlePalmApi'),
    ('connected_tools', 'weather_api', 'متصل عبر HTTP — OpenWeatherMap أو Aladhan')
ON CONFLICT (category, key) DO NOTHING;

-- Category: operational_rules
INSERT INTO othvm_memory (category, key, value) VALUES
    ('operational_rules', 'language_rule', 'جميع الردود والرسائل يجب أن تكون باللغة العربية'),
    ('operational_rules', 'time_format_rule', 'استخدم صيغة 12 ساعة مع صباحاً/مساءً'),
    ('operational_rules', 'confirmation_rule', 'أي عملية كتابة أو إرسال أو حذف خارجية تحتاج تأكيد قبل التنفيذ'),
    ('operational_rules', 'token_efficiency', 'استخدم Gemini Flash للمهام البسيطة و Gemini Pro للمهام المعقدة فقط'),
    ('operational_rules', 'context_loading', 'حمّل core_context في بداية كل محادثة، ثم حمّل الذاكرة الإضافية حسب المهمة'),
    ('operational_rules', 'unauthorized_block', 'حظر أي رسالة من chat_id غير مطابق لمعرف المؤسس'),
    ('operational_rules', 'location', '{{المدينة، المحافظة، البلد — الإحداثيات}}')
ON CONFLICT (category, key) DO NOTHING;
