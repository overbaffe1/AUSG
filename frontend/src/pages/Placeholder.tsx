// Экран ещё не собран. Эндпоинт на бэкенде уже есть — см. ТЗ, раздел 7.
export default function Placeholder(props: { title: string; task: string; endpoint: string }) {
  return (
    <>
      <h2>{props.title}</h2>
      <div className="card">
        <p className="muted">Экран разворачивается на этапе 2-3.</p>
        <table>
          <tbody>
            <tr><td>Задача в ТЗ</td><td><b>{props.task}</b></td></tr>
            <tr><td>Готовый эндпоинт</td><td><code>{props.endpoint}</code></td></tr>
          </tbody>
        </table>
      </div>
    </>
  )
}
