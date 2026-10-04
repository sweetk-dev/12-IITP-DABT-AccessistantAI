package kr.co.sweetk.accessnavi

import android.content.ContentProvider
import android.content.ContentValues
import android.content.Context
import android.database.Cursor
import android.database.MatrixCursor
import android.net.Uri
import android.os.ParcelFileDescriptor
import android.provider.OpenableColumns
import java.io.File
import java.io.FileNotFoundException

/**
 * 내려받은 설치 파일 하나를 시스템 설치 화면에 넘겨 주는 통로.
 *
 * 시스템 설치 화면은 다른 앱이라 우리 앱의 저장소를 직접 읽지 못한다. 설치를 시작할 때 이 주소에 대한
 * 읽기 권한을 그 화면에만 잠깐 준다(exported=false + grantUriPermissions). 내주는 파일은 update.apk 하나뿐이다.
 */
class ApkProvider : ContentProvider() {
    override fun onCreate(): Boolean = true

    private fun target(uri: Uri): File {
        val ctx = context ?: throw FileNotFoundException()
        if (uri.authority != authority(ctx) || uri.path != "/$NAME") throw FileNotFoundException()
        return file(ctx)
    }

    override fun getType(uri: Uri): String = MIME

    override fun openFile(uri: Uri, mode: String): ParcelFileDescriptor {
        if (mode != "r") throw FileNotFoundException()
        val f = target(uri)
        if (!f.isFile) throw FileNotFoundException()
        return ParcelFileDescriptor.open(f, ParcelFileDescriptor.MODE_READ_ONLY)
    }

    override fun query(uri: Uri, projection: Array<out String>?, selection: String?,
                       selectionArgs: Array<out String>?, sortOrder: String?): Cursor {
        val f = target(uri)
        if (!f.isFile) throw FileNotFoundException()
        val cols = projection ?: arrayOf(OpenableColumns.DISPLAY_NAME, OpenableColumns.SIZE)
        val c = MatrixCursor(cols, 1)
        c.addRow(cols.map { when (it) {
            OpenableColumns.DISPLAY_NAME -> NAME
            OpenableColumns.SIZE -> f.length()
            else -> null
        } })
        return c
    }

    override fun insert(uri: Uri, values: ContentValues?): Uri? = null
    override fun delete(uri: Uri, selection: String?, selectionArgs: Array<out String>?): Int = 0
    override fun update(uri: Uri, values: ContentValues?, selection: String?, selectionArgs: Array<out String>?): Int = 0

    companion object {
        const val MIME = "application/vnd.android.package-archive"
        private const val NAME = "update.apk"
        fun authority(ctx: Context) = ctx.packageName + ".apk"
        fun file(ctx: Context) = File(ctx.cacheDir, NAME)
        fun uri(ctx: Context): Uri = Uri.parse("content://" + authority(ctx) + "/" + NAME)
    }
}
