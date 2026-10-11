<script setup lang="ts">
import { useForm } from 'vee-validate'
import { toTypedSchema } from '@vee-validate/zod'
import * as z from 'zod'
import { Loader2 } from '@lucide/vue'
import { toast } from 'vue-sonner'
import { Checkbox } from '@/components/ui/checkbox'
import { Label } from '@/components/ui/label'

definePageMeta({
  layout: 'empty'
})

const { login } = useAuth()
const router = useRouter()
const { $api } = useNuxtApp()
const remembered = useRememberedLogin()

const isLoading = ref(false)
const registrationEnabled = ref(false)
const rememberMe = ref(false)

const formSchema = toTypedSchema(z.object({
  username: z.string().min(1, '请输入用户名'),
  password: z.string().min(1, '请输入密码'),
}))

const form = useForm({
  validationSchema: formSchema,
})

// 若系统尚未完成首次初始化，引导至安装向导；同时回填上次记住的账号密码
onMounted(async () => {
  const saved = remembered.load()
  if (saved) {
    form.setValues({ username: saved.username, password: saved.password })
    rememberMe.value = true
  }

  try {
    const res = await $api<{ configured: boolean }>('/setup/status')
    if (!res.configured) {
      router.replace('/setup')
    }
  } catch {
    // 忽略状态探测失败
  }
  try {
    const cfg = await $api<{ enabled: boolean }>('/register/config')
    registrationEnabled.value = cfg.enabled
  } catch {
    // 忽略注册配置探测失败
  }
})

const onSubmit = form.handleSubmit(async (values) => {
  isLoading.value = true
  try {
    await login({ username: values.username, password: values.password })

    // 登录成功后再落盘，避免把打错的密码记下来
    if (rememberMe.value) {
      remembered.save({ username: values.username, password: values.password })
    } else {
      remembered.clear()
    }

    toast.success('登录成功')
    router.push('/')
  } catch (error: any) {
    const status = error.statusCode ?? error.status ?? error.response?.status
    if (status === 403) {
      toast.error('账号未激活，正在等待管理员审核，请联系管理员')
    } else if (status === 400) {
      toast.error('用户名或密码错误')
    } else {
      toast.error(error.message || '登录失败')
    }
  } finally {
    isLoading.value = false
  }
})

// 取消勾选时立即清除，不必等到下次登录成功
watch(rememberMe, (on) => {
  if (!on) remembered.clear()
})
</script>

<template>
  <div class="flex h-screen w-full items-center justify-center bg-muted/40 px-4">
    <Card class="w-full sm:w-[400px]">
      <CardHeader class="space-y-1">
        <div class="flex justify-center mb-4">
          <img src="/logo.svg" alt="Logo" class="h-16 w-auto" />
        </div>
        <CardTitle class="text-2xl text-center">登录账户</CardTitle>
        <CardDescription class="text-center">
          输入您的用户名和密码进行登录
        </CardDescription>
      </CardHeader>
      <CardContent>
        <form @submit="onSubmit">
          <div class="grid gap-4">
            <FormField v-slot="{ componentField }" name="username">
              <FormItem>
                <FormLabel>用户名</FormLabel>
                <FormControl>
                  <Input
                    type="text"
                    placeholder="admin"
                    autocomplete="username"
                    v-bind="componentField"
                  />
                </FormControl>
                <FormMessage />
              </FormItem>
            </FormField>

            <FormField v-slot="{ componentField }" name="password">
              <FormItem>
                <FormLabel>密码</FormLabel>
                <FormControl>
                  <Input
                    type="password"
                    placeholder="******"
                    autocomplete="current-password"
                    v-bind="componentField"
                  />
                </FormControl>
                <FormMessage />
              </FormItem>
            </FormField>

            <div class="space-y-1">
              <div class="flex items-center gap-2">
                <Checkbox
                  id="remember-me"
                  :model-value="rememberMe"
                  @update:model-value="(v) => (rememberMe = v === true)"
                />
                <Label for="remember-me" class="cursor-pointer text-sm font-normal">
                  记住账号密码
                </Label>
              </div>
              <p class="pl-6 text-xs text-muted-foreground">
                在此浏览器中自动填充。密码以本地混淆方式保存，<strong>非加密</strong>，
                请勿在公用电脑上勾选。
              </p>
            </div>

            <Button type="submit" :disabled="isLoading" class="w-full">
              <Loader2 v-if="isLoading" class="mr-2 h-4 w-4 animate-spin" />
              登录
            </Button>

            <div v-if="registrationEnabled" class="text-center text-sm text-muted-foreground">
              还没有账户？
              <NuxtLink to="/register" class="text-primary hover:underline">立即注册</NuxtLink>
            </div>
          </div>
        </form>
      </CardContent>
    </Card>
  </div>
</template>
