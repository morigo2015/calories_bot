# Workspace and deployment

This workspace is the live checkout on the Hetzner server. The project lives at
`/home/igor/calories-bot` and is owned by `igor:igor`.

- Do not SSH or copy files to another host to deploy changes: edits made here
  are already on the server.
- Do not restart the bot, commit, or push changes automatically after editing.
  The user runs the deployment script themselves.
- After every application-code update, give the user this exact command, with a
  concise commit message appropriate to the changes:

  ```bash
  bash /home/igor/calories-bot/run_update.sh "<commit message>"
  ```

- `run_update.sh` stages the approved project paths, creates a commit when
  needed, pushes it to `origin`, then runs `sudo systemctl restart calories-bot`
  and confirms that the service is active.
- A direct `sudo systemctl` command may require an interactive password. Do not
  try to work around that requirement; provide the deployment command above.
- Never include `.env`, `service-account.json`, `data/`, or spreadsheet exports
  in Git commits. Preserve unrelated user changes and untracked files.

# Testing

- After a significant application update, run `bash scripts/run_tests.sh`.
- Run the paid LLM eval only after the user explicitly approves it in the current
  conversation: `bash scripts/run_tests.sh --llm --confirm`.

# Permissions and sandboxing

- Keep project directories writable by their owner (`u+rwx`) and regular files
  writable by their owner (`u+rw`). Do not remove owner write permissions as a
  hardening step.
- Secrets such as `.env` and `service-account.json` must remain owner-only
  (`600`).

## Перевірка завдання перед виконанням

Перед початком роботи:

1. Перевірити, чи достатньо інформації для коректного виконання завдання.
2. Якщо бракує суттєвої інформації або потрібен вибір користувача, спочатку поставити уточнювальне запитання й не починати реалізацію до отримання відповіді.
3. Якщо є суттєві зауваження, ризики, неоднозначності або технічні обмеження, які можуть вплинути на результат, спочатку повідомити про них користувача й дочекатися рішення, коли без нього небезпечно або недоцільно продовжувати.
4. Якщо інформації достатньо й суттєвих зауважень немає, одразу виконувати завдання без зайвих запитань чи повторного підтвердження.

## Git і версіонування

- Для всіх змін у проєкті використовувати Git.
- Перед редагуванням перевіряти поточний стан робочого дерева та не перезаписувати й не включати до своїх комітів сторонні або незавершені зміни користувача.
- Після завершення та перевірки завдання створювати окремий Git-коміт, що містить лише зміни цього завдання.
- Використовувати короткі, змістовні повідомлення комітів, які пояснюють, що саме було змінено (наприклад: `feat: add replay asset valuation`).
- Для окремих логічних етапів великого завдання створювати окремі коміти, якщо це полегшує перегляд або відкат змін.
- Не переписувати історію Git, не видаляти чужі зміни й не виконувати руйнівні Git-команди без прямого дозволу користувача.
- У фінальній відповіді вказувати хеш і повідомлення створеного коміту. Якщо коміт створити неможливо, чітко пояснити причину.
