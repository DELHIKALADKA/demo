package app.pagesnap

import android.net.Uri
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import android.graphics.BitmapFactory
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.File

/**
 * The app: pick a video or paste a link -> it finds the pages -> tick the ones
 * you want, name the notes, save the PDF. Everything happens on the phone.
 */
class MainActivity : ComponentActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val pipeline = SnapPipeline(this)
        setContent { PageSnapTheme { App(pipeline) } }
    }
}

private sealed interface Screen {
    data object Home : Screen
    data class Working(val stage: String, val fraction: Float) : Screen
    data class Review(val draft: SnapPipeline.Draft) : Screen
    data object Saved : Screen
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun App(pipeline: SnapPipeline) {
    val scope = rememberCoroutineScope()
    var screen by remember { mutableStateOf<Screen>(Screen.Home) }
    var error by remember { mutableStateOf<String?>(null) }
    var link by remember { mutableStateOf("") }
    var sensitivity by remember { mutableStateOf(app.pagesnap.engine.Sensitivity.BALANCED) }
    var lossless by remember { mutableStateOf(false) }
    var savedMessage by remember { mutableStateOf<String?>(null) }

    val pickVideo = rememberLauncherForActivityResult(
        ActivityResultContracts.OpenDocument()
    ) { uri: Uri? ->
        uri ?: return@rememberLauncherForActivityResult
        val name = uri.lastPathSegment?.substringAfterLast('/') ?: "video.mp4"
        val local = copyToCache(pipeline.appContext, uri, name)
        if (local == null) {
            error = "Could not open that video."
            return@rememberLauncherForActivityResult
        }
        scope.launch {
            screen = Screen.Working("Getting ready…", 0f)
            try {
                val draft = pipeline.analyse(local.absolutePath, name, sensitivity) { stage, f ->
                    screen = Screen.Working(stage, f)
                }
                screen = if (draft.pages.isEmpty()) {
                    error = "No pages were found in that video. Try the Eager setting."
                    Screen.Home
                } else Screen.Review(draft)
            } catch (t: Throwable) {
                error = t.message ?: "Something went wrong while reading the video."
                screen = Screen.Home
            }
        }
    }

    Scaffold(
        topBar = {
            TopAppBar(title = {
                Column {
                    Text("PageSnap", fontWeight = FontWeight.SemiBold)
                    Text("video → notes PDF, on your phone",
                        fontSize = 12.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
            })
        },
        snackbarHost = {
            savedMessage?.let {
                Snackbar(Modifier.padding(12.dp), action = {
                    TextButton(onClick = { savedMessage = null }) { Text("OK") }
                }) { Text(it) }
            }
        }
    ) { padding ->
        Column(
            Modifier.padding(padding).fillMaxSize().padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(14.dp)
        ) {
            when (val s = screen) {
                is Screen.Home -> {
                    Card(Modifier.fillMaxWidth()) {
                        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
                            Button(
                                onClick = { pickVideo.launch(arrayOf("video/*")) },
                                modifier = Modifier.fillMaxWidth().height(52.dp)
                            ) { Text("Choose a video from this phone") }

                            OutlinedTextField(
                                value = link, onValueChange = { link = it },
                                label = { Text("…or paste an Instagram / Facebook / YouTube link") },
                                singleLine = true, modifier = Modifier.fillMaxWidth(),
                                keyboardOptions = KeyboardOptions(imeAction = ImeAction.Done)
                            )
                            Button(
                                onClick = {
                                    if (link.isBlank()) { error = "Paste a link first."; return@Button }
                                    scope.launch {
                                        screen = Screen.Working("Downloading the video…", 0.05f)
                                        try {
                                            val file = app.pagesnap.download.VideoFetcher(pipeline.appContext)
                                                .download(link)
                                            val draft = pipeline.analyse(file.absolutePath, file.name, sensitivity) { st, f ->
                                                screen = Screen.Working(st, f)
                                            }
                                            screen = if (draft.pages.isEmpty()) {
                                                error = "No pages were found in that video."
                                                Screen.Home
                                            } else Screen.Review(draft)
                                        } catch (t: Throwable) {
                                            error = t.message ?: "Could not download that link."
                                            screen = Screen.Home
                                        }
                                    }
                                },
                                enabled = link.isNotBlank(),
                                modifier = Modifier.fillMaxWidth().height(48.dp)
                            ) { Text("Download and find the notes") }

                            Divider()
                            Row(verticalAlignment = Alignment.CenterVertically) {
                                Column(Modifier.weight(1f)) {
                                    Text("How eager should it be?", fontSize = 14.sp)
                                    Text(
                                        when (sensitivity) {
                                            app.pagesnap.engine.Sensitivity.STRICT -> "Strict — fewer, sure pages"
                                            app.pagesnap.engine.Sensitivity.BALANCED -> "Balanced — recommended"
                                            app.pagesnap.engine.Sensitivity.EAGER -> "Eager — catch subtle changes"
                                        }, fontSize = 12.sp, color = MaterialTheme.colorScheme.onSurfaceVariant
                                    )
                                }
                                Switch(
                                    checked = sensitivity == app.pagesnap.engine.Sensitivity.EAGER,
                                    onCheckedChange = { eager ->
                                        sensitivity = if (eager) app.pagesnap.engine.Sensitivity.EAGER
                                                      else app.pagesnap.engine.Sensitivity.BALANCED
                                    }
                                )
                            }
                            Row(verticalAlignment = Alignment.CenterVertically) {
                                Column(Modifier.weight(1f)) {
                                    Text("Keep pictures lossless", fontSize = 14.sp)
                                    Text("Bigger PDF, best for diagrams and handwriting",
                                        fontSize = 12.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
                                }
                                Switch(checked = lossless, onCheckedChange = { lossless = it })
                            }
                        }
                    }

                    SavedNotes(pipeline) { screen = Screen.Home }
                }

                is Screen.Working -> {
                    Card(Modifier.fillMaxWidth()) {
                        Column(Modifier.padding(20.dp), verticalArrangement = Arrangement.spacedBy(14.dp)) {
                            Text(s.stage, fontWeight = FontWeight.Medium)
                            LinearProgressIndicator(
                                progress = { s.fraction }, modifier = Modifier.fillMaxWidth()
                            )
                            Text("Screenshots are taken only when a page changes and has finished — "
                                    + "never mid-stroke, never mid-fade.",
                                fontSize = 12.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
                        }
                    }
                }

                is Screen.Review -> {
                    var title by remember(s.draft.id) { mutableStateOf(s.draft.title) }
                    ReviewScreen(
                        pipeline = pipeline,
                        draft = s.draft,
                        title = title,
                        onTitle = { title = it; s.draft.title = it },
                        lossless = lossless,
                        onToggle = { index, keep ->
                            s.draft.pages[index].keep = keep
                            pipeline.persist(s.draft)
                        },
                        onSave = {
                            scope.launch {
                                try {
                                    val (name, uri) = pipeline.savePdf(
                                        s.draft, app.pagesnap.engine.NotesPdf.Options(
                                            pageNumbers = false, grayscale = false,
                                            maxWidthPx = 2200,
                                        )
                                    )
                                    pipeline.persist(s.draft)
                                    savedMessage = "Saved $name to Downloads/PageSnap"
                                } catch (t: Throwable) {
                                    error = t.message ?: "Could not write the PDF."
                                }
                            }
                        },
                        onBack = { screen = Screen.Home },
                    )
                }
                Screen.Saved -> screen = Screen.Home
            }

            error?.let {
                Card(colors = CardDefaults.cardColors(
                    containerColor = MaterialTheme.colorScheme.errorContainer)) {
                    Row(Modifier.padding(14.dp), verticalAlignment = Alignment.CenterVertically) {
                        Text(it, modifier = Modifier.weight(1f), fontSize = 14.sp,
                            color = MaterialTheme.colorScheme.onErrorContainer)
                        TextButton(onClick = { error = null }) { Text("Dismiss") }
                    }
                }
            }
        }
    }
}

@Composable
private fun ReviewScreen(
    pipeline: SnapPipeline,
    draft: SnapPipeline.Draft,
    title: String,
    onTitle: (String) -> Unit,
    lossless: Boolean,
    onToggle: (Int, Boolean) -> Unit,
    onSave: () -> Unit,
    onBack: () -> Unit,
) {
    val kept = draft.pages.count { it.keep }
    Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            IconButton(onClick = onBack) { Text("←", fontSize = 20.sp) }
            Column(Modifier.weight(1f)) {
                Text("${draft.pages.size} page(s) found", fontWeight = FontWeight.SemiBold)
                Text("${"%.1f".format(draft.durationSeconds)}s of video · $kept selected",
                    fontSize = 12.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            TextButton(onClick = { draft.pages.indices.forEach { onToggle(it, true) } }) { Text("All") }
            TextButton(onClick = { draft.pages.indices.forEach { onToggle(it, false) } }) { Text("None") }
        }

        LazyVerticalGrid(
            columns = GridCells.Adaptive(minSize = 108.dp),
            modifier = Modifier.weight(1f),
            verticalArrangement = Arrangement.spacedBy(8.dp),
            horizontalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            items(draft.pages) { page ->
                PageTile(pipeline, draft.id, page) { keep -> onToggle(page.index, keep) }
            }
        }

        OutlinedTextField(
            value = title, onValueChange = onTitle,
            label = { Text("Name these notes") },
            singleLine = true, modifier = Modifier.fillMaxWidth()
        )
        Button(
            onClick = onSave, enabled = kept > 0,
            modifier = Modifier.fillMaxWidth().height(52.dp)
        ) { Text(if (kept == 0) "Tick at least one page" else "Save the PDF ($kept page(s))") }
        Text("Saved to Downloads/PageSnap. Everything stays on this phone.",
            fontSize = 12.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
    }
}

@Composable
private fun PageTile(
    pipeline: SnapPipeline,
    noteId: String,
    page: SnapPipeline.Draft.Page,
    onToggle: (Boolean) -> Unit,
) {
    var bitmap by remember(page.file) { mutableStateOf<android.graphics.Bitmap?>(null) }
    LaunchedEffect(page.file) {
        bitmap = withContext(Dispatchers.IO) {
            val f = pipeline.bitmapFor(noteId, page.file)
            if (f.exists()) BitmapFactory.decodeFile(f.absolutePath) else null
        }
    }
    Box(
        Modifier
            .clip(RoundedCornerShape(10.dp))
            .background(MaterialTheme.colorScheme.surfaceVariant)
            .clickable { onToggle(!page.keep) }
    ) {
        bitmap?.let {
            Image(
                bitmap = it.asImageBitmap(),
                contentDescription = "page ${page.index + 1}",
                contentScale = ContentScale.FillWidth,
                modifier = Modifier.fillMaxWidth().alpha(if (page.keep) 1f else 0.35f)
            )
        } ?: Box(Modifier.fillMaxWidth().height(90.dp))
        Row(
            Modifier.align(Alignment.BottomStart).fillMaxWidth().padding(6.dp),
            verticalAlignment = Alignment.CenterVertically
        ) {
            Text("#${page.index + 1}", fontSize = 11.sp, color = Color.White)
            Spacer(Modifier.weight(1f))
            Text("%.1fs".format(page.time), fontSize = 11.sp, color = Color.White)
        }
        Checkbox(checked = page.keep, onCheckedChange = { onToggle(it) }, modifier = Modifier.align(Alignment.TopEnd))
    }
}

@Composable
private fun SavedNotes(pipeline: SnapPipeline, onChanged: () -> Unit) {
    val notes = remember { mutableStateOf(pipeline.notes()) }
    if (notes.value.isEmpty()) return
    Text("Saved notes", fontWeight = FontWeight.SemiBold)
    LazyColumn(verticalArrangement = Arrangement.spacedBy(8.dp)) {
        items(notes.value) { note ->
            Card {
                Row(Modifier.padding(12.dp), verticalAlignment = Alignment.CenterVertically) {
                    Column(Modifier.weight(1f)) {
                        Text(note.title, maxLines = 1, overflow = TextOverflow.Ellipsis,
                            fontWeight = FontWeight.Medium)
                        Text("${note.keptPages.size} page(s) · from ${note.sourceName}",
                            fontSize = 12.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                    TextButton(onClick = { pipeline.delete(note.id); notes.value = pipeline.notes(); onChanged() }) {
                        Text("Delete")
                    }
                }
            }
        }
    }
}

@Composable
private fun PageSnapTheme(content: @Composable () -> Unit) {
    MaterialTheme(colorScheme = darkColorScheme(
        primary = Color(0xFFE0533F), secondary = Color(0xFFFF7A63),
    ), content = content)
}

// -- small helpers ----------------------------------------------------------

/** Videos picked from the file manager arrive as a Uri; the engine wants a file. */
private fun copyToCache(context: android.content.Context, uri: Uri, name: String): File? {
    return try {
        val target = File(context.cacheDir, "input-" + name.takeLast(40))
        context.contentResolver.openInputStream(uri)?.use { input ->
            target.outputStream().use { output -> input.copyTo(output) }
        }
        target
    } catch (t: Throwable) {
        null
    }
}

