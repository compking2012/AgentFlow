package local.agentflow.reference
import android.content.Intent
import androidx.test.core.app.ActivityScenario
import androidx.test.platform.app.InstrumentationRegistry
import androidx.test.espresso.Espresso.onView
import androidx.test.espresso.action.ViewActions.*
import androidx.test.espresso.assertion.ViewAssertions.matches
import androidx.test.espresso.matcher.ViewMatchers.*
import org.junit.Assert.*
import org.junit.Test
import java.util.UUID

class TicketUiTest {
    @Test fun createAndReopen() {
        val endpoint=InstrumentationRegistry.getArguments().getString("apiBaseUrl")
        assertFalse("A real backend URL is required, not a skipped test",endpoint.isNullOrBlank())
        val context=InstrumentationRegistry.getInstrumentation().targetContext
        val intent=Intent(context,MainActivity::class.java).putExtra("apiBaseUrl",endpoint)
        val title="android-"+UUID.randomUUID()
        ActivityScenario.launch<MainActivity>(intent).use {
            onView(withId(R.id.ticket_title)).perform(typeText(title),closeSoftKeyboard())
            onView(withId(R.id.create_ticket)).perform(click())
            waitForTitle(title)
            it.recreate();waitForTitle(title)
        }
        ActivityScenario.launch<MainActivity>(intent).use { waitForTitle(title) }
    }
    private fun waitForTitle(title:String) {
        val until=System.currentTimeMillis()+15000;var last:Throwable?=null
        while(System.currentTimeMillis()<until){try{onView(withText(title)).check(matches(isDisplayed()));return}catch(e:Throwable){last=e;Thread.sleep(100)}}
        throw AssertionError("Native ticket was not persisted/displayed",last)
    }
}
