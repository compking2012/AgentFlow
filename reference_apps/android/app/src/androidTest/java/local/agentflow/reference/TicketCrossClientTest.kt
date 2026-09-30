package local.agentflow.reference

import android.content.Intent
import androidx.test.core.app.ActivityScenario
import androidx.test.platform.app.InstrumentationRegistry
import androidx.test.espresso.Espresso.onView
import androidx.test.espresso.action.ViewActions.click
import androidx.test.espresso.assertion.ViewAssertions.matches
import androidx.test.espresso.matcher.ViewMatchers.*
import org.hamcrest.Matchers.allOf
import org.junit.Assert.assertFalse
import org.junit.Test

/** Run only after the API coordinator created the declared ticket on the same frozen backend. */
class TicketCrossClientTest {
    @Test fun assignApiTicketUsingNativeControl() {
        val arguments=InstrumentationRegistry.getArguments()
        val endpoint=arguments.getString("apiBaseUrl")
        val title=arguments.getString("crossClientTicketTitle")
        assertFalse("A real frozen API is required",endpoint.isNullOrBlank())
        assertFalse("The preceding API step must supply the ticket title",title.isNullOrBlank())
        val context=InstrumentationRegistry.getInstrumentation().targetContext
        val intent=Intent(context,MainActivity::class.java).putExtra("apiBaseUrl",endpoint)
        ActivityScenario.launch<MainActivity>(intent).use {
            waitFor { onView(withText(title)).check(matches(isDisplayed())) }
            onView(withContentDescription("Assign "+title+" to member")).perform(click())
            waitFor { onView(allOf(withContentDescription("Assignment "+title),withText("Assigned: member"))).check(matches(isDisplayed())) }
            it.recreate()
            waitFor { onView(allOf(withContentDescription("Assignment "+title),withText("Assigned: member"))).check(matches(isDisplayed())) }
        }
    }
    private fun waitFor(assertion:()->Unit) {
        val until=System.currentTimeMillis()+15000;var last:Throwable?=null
        while(System.currentTimeMillis()<until){try{assertion();return}catch(error:Throwable){last=error;Thread.sleep(100)}}
        throw AssertionError("Cross-client native state not observed",last)
    }
}
