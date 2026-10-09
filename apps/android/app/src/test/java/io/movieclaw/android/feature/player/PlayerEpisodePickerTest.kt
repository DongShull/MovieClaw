package io.movieclaw.android.feature.player

import android.app.Application
import androidx.compose.ui.test.assertIsEnabled
import androidx.compose.ui.test.assertIsNotEnabled
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import io.movieclaw.android.core.designsystem.MovieClawTheme
import io.movieclaw.android.core.model.EpisodeView
import io.movieclaw.android.core.playback.PlayTarget
import kotlinx.coroutines.awaitCancellation
import org.junit.Assert.assertEquals
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(application = Application::class, sdk = [34], qualifiers = "w800dp-h360dp-mdpi")
class PlayerEpisodePickerTest {
    @get:Rule val compose = createComposeRule()
    private val target = PlayTarget(379, 3, "tv", "仙逆", seasonNumber = 1, episodeNumber = 159)

    @Test fun `current and missing episodes cannot switch but owned episodes can`() {
        var selected: Pair<Int, Int>? = null
        compose.setContent {
            MovieClawTheme {
                PlayerEpisodePicker(target, { listOf(1, 2) }, { _, _ ->
                    listOf(EpisodeView(159, owned = true), EpisodeView(160), EpisodeView(161, owned = true))
                }, {}, { season, episode -> selected = season to episode.episodeNumber })
            }
        }
        compose.onNodeWithText("159").assertIsNotEnabled()
        compose.onNodeWithText("160").assertIsNotEnabled()
        compose.onNodeWithText("161").assertIsEnabled().performClick()
        assertEquals(1 to 161, selected)
    }

    @Test fun `switching seasons cancels an unfinished load`() {
        var selected: Pair<Int, Int>? = null
        compose.setContent {
            MovieClawTheme {
                PlayerEpisodePicker(target, { listOf(1, 2) }, { _, season ->
                    if (season == 1) awaitCancellation()
                    listOf(EpisodeView(1, owned = true))
                }, {}, { season, episode -> selected = season to episode.episodeNumber })
            }
        }
        compose.onNodeWithText("第 2 季").performClick()
        compose.onNodeWithText("1").performClick()
        assertEquals(2 to 1, selected)
    }

    @Test fun `failed requests can be retried without closing the picker`() {
        var attempts = 0
        compose.setContent {
            MovieClawTheme {
                PlayerEpisodePicker(target, { listOf(1) }, { _, _ ->
                    if (++attempts == 1) error("offline")
                    listOf(EpisodeView(161, owned = true))
                }, {}, { _, _ -> })
            }
        }
        compose.onNodeWithText("加载失败，点击重试").performClick()
        compose.onNodeWithText("161").assertIsEnabled()
        assertEquals(2, attempts)
    }
}
