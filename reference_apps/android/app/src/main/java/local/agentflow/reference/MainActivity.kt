package local.agentflow.reference
import android.app.Activity
import android.os.Bundle
import android.widget.*
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL

class MainActivity: Activity() {
    private lateinit var titleInput: EditText
    private lateinit var tickets: LinearLayout
    private lateinit var error: TextView
    private val endpoint get() = intent.getStringExtra("apiBaseUrl") ?: "http://10.0.2.2:8765"
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val layout=LinearLayout(this).apply { orientation=LinearLayout.VERTICAL; setPadding(24,24,24,24) }
        layout.addView(TextView(this).apply { text="AgentFlow Tickets"; textSize=24f })
        titleInput=EditText(this).apply { id=R.id.ticket_title; hint="Ticket title" }
        error=TextView(this).apply { id=R.id.error_message }
        tickets=LinearLayout(this).apply { id=R.id.ticket_list; orientation=LinearLayout.VERTICAL }
        layout.addView(titleInput)
        layout.addView(Button(this).apply { id=R.id.create_ticket; text="Create ticket"; setOnClickListener { create() } })
        layout.addView(Button(this).apply { id=R.id.refresh_tickets; text="Refresh"; setOnClickListener { refresh() } })
        layout.addView(error); layout.addView(tickets); setContentView(layout); refresh()
    }
    private fun request(path:String, payload:JSONObject?=null):JSONObject {
        val connection=URL(endpoint+path).openConnection() as HttpURLConnection
        connection.connectTimeout=5000; connection.readTimeout=5000
        connection.setRequestProperty("Authorization","Bearer reference."+(intent.getStringExtra("role") ?: "manager"))
        if(payload!=null) { connection.requestMethod="POST"; connection.doOutput=true; connection.setRequestProperty("Content-Type","application/json"); connection.outputStream.use { it.write(payload.toString().toByteArray()) } }
        try {
            val ok=connection.responseCode in 200..299
            val body=(if(ok)connection.inputStream else connection.errorStream).bufferedReader().use { it.readText() }
            val parsed=JSONObject(body); if(!ok)throw IllegalStateException(parsed.optString("error","request_failed")); return parsed
        } finally { connection.disconnect() }
    }
    private fun refresh() {
        Thread {
            try { val items=request("/api/tickets").getJSONArray("tickets"); runOnUiThread {
                tickets.removeAllViews()
                for(i in 0 until items.length()) {
                    val ticket=items.getJSONObject(i); val title=ticket.getString("title")
                    val row=LinearLayout(this).apply { orientation=LinearLayout.VERTICAL }
                    row.addView(TextView(this).apply { text=title; textSize=18f })
                    row.addView(TextView(this).apply { text="Assigned: "+(if(ticket.isNull("assignee"))"none" else ticket.getString("assignee")); contentDescription="Assignment "+title })
                    for(assignee in listOf("member","manager"))row.addView(Button(this).apply {
                        text="Assign to "+assignee; contentDescription="Assign "+title+" to "+assignee
                        setOnClickListener { assign(ticket.getLong("id"),assignee) }
                    })
                    tickets.addView(row)
                }
                error.text=""
            } }
            catch(e:Exception) { runOnUiThread { error.text=e.message } }
        }.start()
    }
    private fun assign(id:Long,assignee:String) {
        Thread {
            try { request("/api/tickets/"+id+"/assign",JSONObject().put("assignee",assignee));runOnUiThread { refresh() } }
            catch(e:Exception){runOnUiThread {error.text=e.message}}
        }.start()
    }
    private fun create() {
        val value=titleInput.text.toString()
        if(!TicketRules.validTitle(value)){error.text="invalid_title";return}
        Thread {
            try {request("/api/tickets",JSONObject().put("title",value));runOnUiThread {titleInput.setText("");refresh()} }
            catch(e:Exception){runOnUiThread {error.text=e.message}}
        }.start()
    }
}
